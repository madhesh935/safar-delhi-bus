"""Static GTFS -> transit network: stops, route patterns with geometry, road segments, schedules."""
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from .geo import Polyline, GridIndex, to_xy


def _read(gtfs_dir: Path, name: str, required=True):
    p = Path(gtfs_dir) / name
    if not p.exists():
        if required:
            raise FileNotFoundError(f"GTFS file missing: {p}")
        return None
    return pd.read_csv(p, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8-sig")


def hms_to_s(series: pd.Series) -> np.ndarray:
    """'25:10:00' -> 90600 (GTFS allows > 24h). Vectorised."""
    parts = series.fillna("").str.strip().str.split(":", expand=True)
    if parts.shape[1] < 3:
        return np.full(len(series), np.nan)
    h = pd.to_numeric(parts[0], errors="coerce")
    m = pd.to_numeric(parts[1], errors="coerce")
    s = pd.to_numeric(parts[2], errors="coerce")
    return (h * 3600 + m * 60 + s).to_numpy(float)


class Network:
    """Everything static about the bus network, prepared for fast matching and routing."""

    # ------------------------------------------------------------------ build
    @classmethod
    def from_gtfs(cls, gtfs_dir, verbose=True):
        self = cls()
        log = print if verbose else (lambda *a, **k: None)
        gtfs_dir = Path(gtfs_dir)

        # ---- stops
        stops = _read(gtfs_dir, "stops.txt")
        stops["lat"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
        stops["lon"] = pd.to_numeric(stops["stop_lon"], errors="coerce")
        stops = stops.dropna(subset=["lat", "lon"]).reset_index(drop=True)
        if "stop_name" not in stops:
            stops["stop_name"] = stops["stop_id"]
        stops["stop_name"] = stops["stop_name"].fillna(stops["stop_id"])
        stops["x"], stops["y"] = to_xy(stops["lat"].to_numpy(), stops["lon"].to_numpy())
        self.stops = stops[["stop_id", "stop_name", "lat", "lon", "x", "y"]].copy()
        self.stop_index = {sid: i for i, sid in enumerate(self.stops["stop_id"])}
        log(f"  stops: {len(self.stops):,}")

        # ---- routes
        routes = _read(gtfs_dir, "routes.txt")
        for c in ("route_short_name", "route_long_name"):
            if c not in routes:
                routes[c] = ""
        self.routes = {
            r.route_id: {
                "short": (r.route_short_name if isinstance(r.route_short_name, str) and r.route_short_name else r.route_id),
                "long": (r.route_long_name if isinstance(r.route_long_name, str) else ""),
            }
            for r in routes.itertuples()
        }
        log(f"  routes: {len(self.routes):,}")

        # ---- trips + calendar
        trips = _read(gtfs_dir, "trips.txt")
        for c in ("service_id", "shape_id", "direction_id"):
            if c not in trips:
                trips[c] = ""
        cal = _read(gtfs_dir, "calendar.txt", required=False)
        flags = {}
        if cal is not None and len(cal):
            for r in cal.itertuples():
                wk = any(getattr(r, d, "0") == "1" for d in ("monday", "tuesday", "wednesday", "thursday", "friday"))
                flags[r.service_id] = (wk, getattr(r, "saturday", "0") == "1", getattr(r, "sunday", "0") == "1")
        trips["wk"] = [flags.get(s, (True, True, True))[0] for s in trips["service_id"]]
        trips["sat"] = [flags.get(s, (True, True, True))[1] for s in trips["service_id"]]
        trips["sun"] = [flags.get(s, (True, True, True))[2] for s in trips["service_id"]]

        # ---- stop_times
        st = _read(gtfs_dir, "stop_times.txt")
        st = st[st["stop_id"].isin(self.stop_index.keys())].copy()
        st["seq"] = pd.to_numeric(st["stop_sequence"], errors="coerce")
        arr = hms_to_s(st["arrival_time"]) if "arrival_time" in st else np.full(len(st), np.nan)
        dep = hms_to_s(st["departure_time"]) if "departure_time" in st else arr
        st["t"] = np.where(np.isnan(arr), dep, arr)
        st["sidx"] = st["stop_id"].map(self.stop_index).astype(np.int64)
        st = st.sort_values(["trip_id", "seq"], kind="stable").reset_index(drop=True)
        if st["t"].isna().any():
            st["t"] = st.groupby("trip_id")["t"].transform(lambda s: s.interpolate(limit_direction="both"))
        st = st.dropna(subset=["t"])
        log(f"  stop_times: {len(st):,}")

        # ---- patterns (unique stop sequences)
        g = st.groupby("trip_id", sort=False)
        seq = g["sidx"].agg(tuple)
        first_t = g["t"].first()
        tinfo = trips.set_index("trip_id")
        seq = seq[seq.map(len) >= 2]
        seq = seq[seq.index.isin(tinfo.index)]
        keyframe = pd.DataFrame({"seq": seq, "route_id": tinfo.loc[seq.index, "route_id"].to_numpy()})
        keyframe["key"] = list(zip(keyframe["route_id"], keyframe["seq"]))
        uniq = {k: i for i, k in enumerate(pd.unique(keyframe["key"]))}
        keyframe["pattern"] = keyframe["key"].map(uniq)
        log(f"  trips: {len(keyframe):,}  patterns: {len(uniq):,}")

        # ---- shapes
        shapes = {}
        sh = _read(gtfs_dir, "shapes.txt", required=False)
        if sh is not None and len(sh):
            sh["seq"] = pd.to_numeric(sh["shape_pt_sequence"], errors="coerce")
            sh["lat"] = pd.to_numeric(sh["shape_pt_lat"], errors="coerce")
            sh["lon"] = pd.to_numeric(sh["shape_pt_lon"], errors="coerce")
            sh = sh.dropna(subset=["seq", "lat", "lon"]).sort_values(["shape_id", "seq"])
            for sid, d in sh.groupby("shape_id", sort=False):
                shapes[sid] = d[["lat", "lon"]].to_numpy()

        # ---- per-pattern geometry, stop distances and scheduled times
        trip_rows = g.indices                                 # trip_id -> row positions in st
        t_arr = st["t"].to_numpy(float)
        sx, sy = self.stops["x"].to_numpy(), self.stops["y"].to_numpy()
        self.patterns = []
        self.route_patterns = {}
        seg_lookup = {}
        seg_from, seg_to, seg_lens, seg_sched = [], [], [], []
        n_shape = 0
        for key, pid in uniq.items():
            route_id, sequence = key
            sidx = np.array(sequence, dtype=np.int64)
            ptrips = keyframe.index[keyframe["pattern"].to_numpy() == pid]
            ptinfo = tinfo.loc[ptrips]
            # geometry
            geom, dist, tol = None, None, None
            shape_ids = ptinfo["shape_id"].dropna()
            shape_ids = shape_ids[shape_ids != ""]
            if len(shape_ids):
                sid = shape_ids.mode().iloc[0]
                if sid in shapes and len(shapes[sid]) >= 2:
                    gx, gy = to_xy(shapes[sid][:, 0], shapes[sid][:, 1])
                    pl = Polyline(gx, gy)
                    dd, off = pl.project_sequential(sx[sidx], sy[sidx])
                    if np.median(off) < 60 and np.mean(off > 150) < 0.2:
                        geom, dist, tol = pl, dd, "shape"
                        n_shape += 1
            if geom is None:
                pl = Polyline(sx[sidx], sy[sidx])
                dd, _ = pl.project_sequential(sx[sidx], sy[sidx], tol=5.0)
                geom, dist, tol = pl, dd, "stops"
            dist = np.asarray(dist, float).copy()
            for i in range(1, len(dist)):
                if dist[i] <= dist[i - 1]:
                    dist[i] = dist[i - 1] + 1.0
            # scheduled times matrix (trips x stops), sorted by first departure
            order = np.argsort(first_t.loc[ptrips].to_numpy())
            ptrips = ptrips[order]
            mat = np.vstack([t_arr[trip_rows[t]] for t in ptrips])
            # segments
            seg_ids = np.empty(len(sidx) - 1, dtype=np.int64)
            sched_med = np.nanmedian(np.diff(mat, axis=1), axis=0)
            for i in range(len(sidx) - 1):
                k = (int(sidx[i]), int(sidx[i + 1]))
                if k not in seg_lookup:
                    seg_lookup[k] = len(seg_from)
                    seg_from.append(k[0]); seg_to.append(k[1])
                    seg_lens.append([]); seg_sched.append([])
                s = seg_lookup[k]
                seg_ids[i] = s
                seg_lens[s].append(dist[i + 1] - dist[i])
                seg_sched[s].append(sched_med[i])
            p = {
                "route_id": route_id,
                "stops": sidx,
                "dist": dist,
                "segs": seg_ids,
                "geom": geom,
                "geom_type": tol,
                "trip_ids": np.array(ptrips),
                "sched": mat,
                "wk": ptinfo.loc[ptrips, "wk"].to_numpy(bool),
                "sat": ptinfo.loc[ptrips, "sat"].to_numpy(bool),
                "sun": ptinfo.loc[ptrips, "sun"].to_numpy(bool),
            }
            self.route_patterns.setdefault(route_id, []).append(len(self.patterns))
            self.patterns.append(p)

        self.seg_lookup = seg_lookup
        self.seg_from = np.array(seg_from, dtype=np.int64)
        self.seg_to = np.array(seg_to, dtype=np.int64)
        self.seg_len = np.array([np.median(v) for v in seg_lens])
        self.seg_sched = np.array([np.nanmedian(v) if len(v) else np.nan for v in seg_sched])
        log(f"  segments: {len(self.seg_from):,}  (patterns with road shape: {n_shape:,}/{len(self.patterns):,})")
        self._post()
        return self

    def _post(self):
        """Derived indexes (rebuilt after unpickling)."""
        self.grid = GridIndex(self.stops["x"].to_numpy(), self.stops["y"].to_numpy(), cell=250.0)
        self.stop_patterns = [[] for _ in range(len(self.stops))]
        for pid, p in enumerate(self.patterns):
            for pos, s in enumerate(p["stops"][:-1]):        # cannot board at last stop
                self.stop_patterns[int(s)].append((pid, pos))
        names = self.stops["stop_name"].str.lower().str.replace(r"[^a-z0-9 ]", " ", regex=True)
        self._names = names.str.split().str.join(" ").to_numpy()

    # ------------------------------------------------------------------ persistence
    def save(self, path):
        d = self.__dict__.copy()
        for k in ("grid", "stop_patterns", "_names"):
            d.pop(k, None)
        with open(path, "wb") as f:
            pickle.dump(d, f, protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def load(cls, path):
        self = cls()
        with open(path, "rb") as f:
            self.__dict__.update(pickle.load(f))
        self._post()
        return self

    # ------------------------------------------------------------------ helpers
    def route_name(self, route_id):
        r = self.routes.get(route_id, {})
        return r.get("short", route_id), r.get("long", "")

    def search_stops(self, q, limit=10):
        import re
        toks = re.sub(r"[^a-z0-9 ]", " ", q.lower()).split()
        if not toks:
            return []
        m = np.ones(len(self._names), bool)
        for t in toks:
            m &= np.char.find(self._names.astype(str), t) >= 0
        idx = np.where(m)[0]
        if not len(idx):
            return []
        lens = np.array([len(self._names[i]) for i in idx])
        idx = idx[np.argsort(lens, kind="stable")]
        out, seen = [], set()
        for i in idx:
            name = self.stops.at[i, "stop_name"]
            if name in seen:
                continue
            seen.add(name)
            same = np.where(self.stops["stop_name"].to_numpy() == name)[0]
            out.append({"name": name, "lat": float(self.stops["lat"].iloc[same].mean()),
                        "lon": float(self.stops["lon"].iloc[same].mean()), "n_stop_ids": int(len(same))})
            if len(out) >= limit:
                break
        return out
