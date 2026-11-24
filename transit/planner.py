"""Journey planner: place resolution, RAPTOR search, rich option descriptions, profiles, congestion map."""
import re
from datetime import datetime
from pathlib import Path

import numpy as np

import config
from . import gps
from .geo import haversine, to_xy, to_latlon
from .gtfs import Network
from .model import TravelTimeModel, tbin
from .router import Raptor, Timetables

LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
DAYNAMES = ["weekday", "saturday", "sunday/holiday"]


def hhmm(t):
    t = int(round(t)) % 86400
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}"


def mins(s):
    return round(float(s) / 60.0, 1)


class Planner:
    def __init__(self, artifacts=config.ARTIFACTS, live_mode=config.LIVE_MODE):
        artifacts = Path(artifacts)
        self.net = Network.load(artifacts / "network.pkl")
        self.model = TravelTimeModel.load(artifacts / "model.npz")
        self.tts = Timetables(self.net, self.model)
        self.raptor = Raptor(self.net)
        self.live = None
        if live_mode and live_mode != "off":
            from .live import LiveNowcaster
            self.live = LiveNowcaster(self.net, self.model, live_mode)

    # ------------------------------------------------------------------ places
    def resolve(self, q):
        m = LATLON.match(q or "")
        if m:
            return float(m[1]), float(m[2]), f"{float(m[1]):.5f}, {float(m[2]):.5f}"
        hits = self.net.search_stops(q, limit=1)
        if not hits:
            raise ValueError(f"Place not found: '{q}'. Use 'lat,lng' or part of a stop name.")
        h = hits[0]
        return h["lat"], h["lon"], h["name"]

    def access(self, lat, lon):
        x, y = to_xy(lat, lon)
        for radius in (config.ACCESS_RADIUS_M, 1500, 2500):
            idx, d = self.net.grid.query(float(x), float(y), radius)
            if len(idx):
                break
        idx, d = idx[:40], d[:40]
        return {int(i): float(dd * config.WALK_DETOUR / config.WALK_SPEED_MS) for i, dd in zip(idx, d)}

    # ------------------------------------------------------------------ planning
    def plan(self, frm, to, depart: datetime, max_transfers=config.MAX_TRANSFERS, n_options=3):
        olat, olon, oname = self.resolve(frm)
        dlat, dlon, dname = self.resolve(to)
        dtype = gps.daytype(depart.date())
        t0 = depart.hour * 3600 + depart.minute * 60 + depart.second
        tt = self.tts.get(dtype)
        orig = self.access(olat, olon)
        targ = self.access(dlat, dlon)
        origin = {s: t0 + w for s, w in orig.items()}

        mult, live_used = None, False
        if self.live is not None:
            self.live.refresh()
            now = self.live.now_ist()
            now_t = now.hour * 3600 + now.minute * 60
            if now.date() == depart.date() and abs(now_t - t0) <= 2700:
                mult, live_used = self.live.multiplier_fn(now_t), True

        journeys, seen, banned = [], set(), set()
        for _ in range(3):                                     # diversity: re-run banning used patterns
            res = self.raptor.query(tt, origin, targ, max_transfers=max_transfers, banned=frozenset(banned))
            for j in res:
                key = tuple((l["pattern"], l["board"], l["alight"]) for l in j["legs"] if l["type"] == "bus")
                if key not in seen:
                    seen.add(key)
                    journeys.append(j)
            if not res:
                break
            fastest = min(res, key=lambda j: j["arrival"])
            banned.update(l["pattern"] for l in fastest["legs"] if l["type"] == "bus")

        if not journeys and max_transfers < 3:                 # sparse area: allow one more transfer
            journeys = self.raptor.query(tt, origin, targ, max_transfers=3)
        opts = [self._describe(j, tt, dtype, t0, orig, targ, mult) for j in journeys]
        opts = [o for o in opts if o]
        opts.sort(key=lambda o: (o["total_min"], o["transfers"]))
        best = opts[0]["total_min"] if opts else None
        opts = [o for o in opts if best is None or o["total_min"] <= best + 35][:n_options]
        for i, o in enumerate(opts):
            o["rank"] = i + 1
        walk_only = haversine(olat, olon, dlat, dlon) * config.WALK_DETOUR / config.WALK_SPEED_MS
        return {
            "query": {"from": {"name": oname, "lat": olat, "lon": olon},
                      "to": {"name": dname, "lat": dlat, "lon": dlon},
                      "depart": depart.strftime("%Y-%m-%d %H:%M"), "day_type": DAYNAMES[dtype]},
            "live_adjusted": live_used,
            "walk_only_min": mins(walk_only),
            "quiet_time": hhmm(self.model.quiet_time(dtype)),
            "options": opts,
        }

    def _bus_leg(self, p_id, trip, board, alight, tt, dtype, t_at_stop, mult):
        net, model = self.net, self.model
        p = net.patterns[p_id]
        A = tt["pred"][p_id]
        dep = float(A[trip, board])
        times = A[trip, board:alight + 1].astype(float)
        segs = p["segs"][board:alight]
        if mult is not None:
            times = model.propagate(segs, dep, dtype, mult)
        arr = float(times[-1])
        sd = model.ride_sd(segs, times, dtype)
        S = tt["sched"][p_id]
        sched_ride = float(S[trip, alight] - S[trip, board])
        q = model.quiet_time(dtype)
        quiet = model.propagate(segs, q, dtype)
        hw, ew = model.observed_frequency(p_id, dtype, dep)
        short, long = net.route_name(p["route_id"])
        st = net.stops
        bs, as_ = int(p["stops"][board]), int(p["stops"][alight])
        xs, ys = p["geom"].slice_xy(p["dist"][board], p["dist"][alight])
        if len(xs) > 250:
            k = np.linspace(0, len(xs) - 1, 250).astype(int)
            xs, ys = xs[k], ys[k]
        lat, lon = to_latlon(xs, ys)
        observed = int(model.cnt[segs, dtype, tbin(times[:-1])].astype(bool).sum())
        return {
            "type": "bus", "pattern": int(p_id), "board_pos": int(board), "alight_pos": int(alight),
            "route": short, "route_long_name": long, "route_id": p["route_id"],
            "board_stop": st.at[bs, "stop_name"], "board_lat": float(st.at[bs, "lat"]), "board_lon": float(st.at[bs, "lon"]),
            "alight_stop": st.at[as_, "stop_name"], "alight_lat": float(st.at[as_, "lat"]), "alight_lon": float(st.at[as_, "lon"]),
            "depart": hhmm(dep), "arrive": hhmm(arr),
            "wait_min": mins(max(0.0, dep - t_at_stop)),
            "ride_min": mins(arr - dep), "ride_sd_min": mins(sd),
            "timetable_ride_min": mins(sched_ride), "quiet_hour_ride_min": mins(quiet[-1] - quiet[0]),
            "n_stops": int(alight - board),
            "segments_with_gps_data_pct": round(100.0 * observed / max(1, len(segs)), 0),
            "observed_headway_min": mins(hw) if hw else None,
            "observed_expected_wait_min": mins(ew) if ew else None,
            "path": [[round(float(a), 6), round(float(b), 6)] for a, b in zip(lat, lon)],
            "_dep": dep, "_arr": arr, "_sd": sd, "_sched": sched_ride, "_quiet": float(quiet[-1] - quiet[0]),
        }

    def _describe(self, j, tt, dtype, t0, orig, targ, mult):
        st = self.net.stops
        legs = []
        first = j["first_stop"]
        walk1 = orig[first]
        legs.append({"type": "walk", "to": st.at[first, "stop_name"], "min": mins(walk1)})
        t = t0 + walk1
        rides, sds, scheds, quiets, waits, walks = 0.0, [], 0.0, 0.0, 0.0, walk1
        for l in j["legs"]:
            if l["type"] == "transfer_walk":
                legs.append({"type": "walk", "to": st.at[l["to"], "stop_name"], "min": mins(l["sec"])})
                t += l["sec"]
                walks += l["sec"]
                continue
            b = self._bus_leg(l["pattern"], l["trip"], l["board"], l["alight"], tt, dtype, t, mult)
            if b["_dep"] < t - 1:                         # live delay made us miss it -> skip option
                return None
            waits += b["_dep"] - t
            rides += b["_arr"] - b["_dep"]
            sds.append(b["_sd"])
            scheds += b["_sched"]
            quiets += b["_quiet"]
            t = b["_arr"]
            legs.append({k: v for k, v in b.items() if not k.startswith("_")})
        last = j["last_stop"]
        walk2 = targ[last]
        legs.append({"type": "walk", "to": "destination", "min": mins(walk2)})
        walks += walk2
        t += walk2
        total = t - t0
        sd = float(np.sqrt(np.sum(np.square(sds))))
        n_bus = sum(1 for l in legs if l["type"] == "bus")
        return {
            "summary": " → ".join(l["route"] for l in legs if l["type"] == "bus"),
            "depart": hhmm(t0), "arrive": hhmm(t),
            "total_min": mins(total),
            "range_min": [mins(total - 1.2816 * sd), mins(total + 1.2816 * sd)],
            "walk_min": mins(walks), "wait_min": mins(waits), "ride_min": mins(rides),
            "transfers": n_bus - 1,
            "usual_at_quiet_hour_min": mins(total - rides + quiets),
            "traffic_delay_min": mins(rides - quiets),
            "timetable_estimate_min": mins(total - rides + scheds),
            "legs": legs,
        }

    # ------------------------------------------------------------------ profile
    def profile(self, frm, to, day: datetime, step_min=15):
        """Door-to-door time across the day along the best route found at `day` (its time)."""
        res = self.plan(frm, to, day, n_options=1)
        if not res["options"]:
            return {"times": [], "predicted_min": [], "timetable_min": []}
        dtype = gps.daytype(day.date())
        tt = self.tts.get(dtype)
        opt = res["options"][0]
        out_t, pred, sched, lo, hi = [], [], [], [], []
        for t0 in range(5 * 3600, 23 * 3600 + 1, step_min * 60):
            t = ts = float(t0)
            sd2, ok = 0.0, True
            for l in opt["legs"]:
                if l["type"] == "walk":
                    t += l["min"] * 60
                    ts += l["min"] * 60
                    continue
                pid, b, a = l["pattern"], l["board_pos"], l["alight_pos"]
                A, S = tt["pred"][pid], tt["sched"][pid]
                j = int(np.searchsorted(A[:, b], t)) if len(A) else 0
                js = int(np.searchsorted(S[:, b], ts)) if len(S) else 0
                if j >= len(A) or js >= len(S):
                    ok = False
                    break
                segs = self.net.patterns[pid]["segs"][b:a]
                sd2 += self.model.ride_sd(segs, A[j, b:a + 1].astype(float), dtype) ** 2
                t, ts = float(A[j, a]), float(S[js, a])
            if not ok:
                continue
            sd = np.sqrt(sd2)
            out_t.append(hhmm(t0))
            pred.append(mins(t - t0))
            sched.append(mins(ts - t0))
            lo.append(mins(t - t0 - 1.2816 * sd))
            hi.append(mins(t - t0 + 1.2816 * sd))
        return {"route": opt["summary"], "times": out_t, "predicted_min": pred, "p10_min": lo, "p90_min": hi,
                "timetable_min": sched}

    # ------------------------------------------------------------------ congestion map
    def congestion(self, dtype, t):
        b = int(t // config.BIN_S) % config.N_BINS
        m = self.model
        idx = np.flatnonzero(m.cnt[:, dtype, b] > 0)
        if len(idx) > 40000:
            idx = idx[np.argsort(-m.cnt[idx, dtype, b])[:40000]]
        st = self.net.stops
        f, g = self.net.seg_from[idx], self.net.seg_to[idx]
        lat, lon = st["lat"].to_numpy(), st["lon"].to_numpy()
        ratio = m.freeflow[idx] / m.mu[idx, dtype, b]
        kmh = 3.6 * self.net.seg_len[idx] / m.mu[idx, dtype, b]
        return {
            "bin": hhmm(b * config.BIN_S), "day_type": DAYNAMES[dtype],
            "city_congestion_index": round(float(np.exp(m.F[dtype, b] - m.F[dtype].min())), 2),
            "segments": [[round(float(lat[a]), 5), round(float(lon[a]), 5), round(float(lat[c]), 5),
                          round(float(lon[c]), 5), round(float(r), 2), round(float(k), 1)]
                         for a, c, r, k in zip(f, g, ratio, kmh)],
        }

    def city_profile(self):
        """Congestion index by time of day per day type (for the chart)."""
        F = self.model.F
        return {DAYNAMES[d]: [round(float(np.exp(F[d, b] - F[d].min())), 3) for b in range(config.N_BINS)]
                for d in range(3)}
