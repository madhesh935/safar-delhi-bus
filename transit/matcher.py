"""
Map-matching: raw GPS points -> runs along a route pattern -> stop-crossing times -> segment traversals.

For every vehicle, consecutive points on the same route form a block. Each block is projected onto
every pattern (direction / variant) of that route. A point is accepted if it lies close to the route
geometry; a run is a maximal sequence of accepted points that keeps moving *forward* along the
pattern. Stop-crossing times are interpolated where the (monotonised) distance-along-route passes each
stop. Consecutive crossings give stop-to-stop travel times ("traversals") that include dwell and
signal delay, i.e. exactly what a passenger experiences.
"""
import numpy as np
import pandas as pd

import config

K_SMOOTH = 3


def _runs_on_pattern(p, x, y, t):
    tol = config.MAX_OFFSET_SHAPE_M if p["geom_type"] == "shape" else config.MAX_OFFSET_STOPLINE_M
    s, d = p["geom"].project(x, y)
    idx = np.flatnonzero(d < tol)
    if len(idx) < 4:
        return []
    ss, tt = s[idx], t[idx]
    n = len(ss)
    a = np.clip(np.arange(n) + K_SMOOTH, 0, n - 1)
    b = np.clip(np.arange(n) - K_SMOOTH, 0, n - 1)
    good = (ss[a] - ss[b]) >= -30.0                       # moving forward (or standing)
    keep = np.flatnonzero(good)
    if len(keep) < 4:
        return []
    ki = idx[keep]
    s2, t2 = ss[keep], tt[keep]
    brk = np.r_[True, (np.diff(ki) > 3) | (np.diff(t2) > config.MAX_GAP_S) | (np.diff(s2) < -200.0)]
    rid = np.cumsum(brk)
    starts = np.flatnonzero(brk)
    ends = np.r_[starts[1:], len(s2)]
    out = []
    for a0, b0 in zip(starts, ends):
        if b0 - a0 < 3:
            continue
        sr, tr = s2[a0:b0], t2[a0:b0]
        if sr.max() - sr.min() < 300:
            continue
        out.append((sr, tr))
    return out


def _crossings(p, sr, tr):
    sm = np.maximum.accumulate(sr)
    D = p["dist"]
    idx = np.searchsorted(sm, D, side="left")
    valid = (idx >= 1) & (idx < len(sm))
    c = np.full(len(D), np.nan)
    if not valid.any():
        return c
    i = idx[valid]
    s0, s1 = sm[i - 1], sm[i]
    t0, t1 = tr[i - 1], tr[i]
    frac = (D[valid] - s0) / np.maximum(s1 - s0, 1e-6)
    cv = t0 + frac * (t1 - t0)
    cv[(t1 - t0) > config.MAX_BRACKET_GAP_S] = np.nan
    c[valid] = cv
    return c


def match_points(df, net, day_start_epoch, run_offset=0, trip_to_pattern=None):
    """df: cleaned points (vehicle, ts, x, y, route, trip) sorted by vehicle, ts.
    Returns traversal DataFrame: run, pattern, pos, seg, t (s after 00:00 IST), dt (s)."""
    if df.empty:
        return _empty()
    v = df["vehicle"].to_numpy()
    r = df["route"].to_numpy()
    ts = df["ts"].to_numpy(float) - day_start_epoch
    x, y = df["x"].to_numpy(), df["y"].to_numpy()
    trip = df["trip"].to_numpy() if "trip" in df else np.array([""] * len(df))
    brk = np.r_[True, (v[1:] != v[:-1]) | (r[1:] != r[:-1]) | (np.diff(ts) > 3 * config.MAX_GAP_S)]
    starts = np.flatnonzero(brk)
    ends = np.r_[starts[1:], len(v)]

    recs = {k: [] for k in ("run", "pattern", "pos", "seg", "t", "dt")}
    run_id = run_offset
    for a, b in zip(starts, ends):
        if b - a < 5:
            continue
        pids = list(net.route_patterns.get(r[a], []))
        if not pids and trip_to_pattern is not None:
            tp = trip_to_pattern.get(trip[a])
            if tp is not None:
                pids = [tp]
        if not pids:
            continue
        cands = []
        for pid in pids:
            p = net.patterns[pid]
            for sr, tr in _runs_on_pattern(p, x[a:b], y[a:b], ts[a:b]):
                c = _crossings(p, sr, tr)
                nc = int(np.isfinite(c).sum())
                if nc >= 2:
                    cands.append((nc, pid, c, tr[0], tr[-1]))
        cands.sort(key=lambda z: -z[0])
        accepted = []
        for nc, pid, c, t0, t1 in cands:
            dur = max(t1 - t0, 1.0)
            if any(max(0.0, min(t1, e) - max(t0, s)) / dur > 0.2 for s, e in accepted):
                continue
            accepted.append((t0, t1))
            p = net.patterns[pid]
            ok = np.isfinite(c[:-1]) & np.isfinite(c[1:])
            pos = np.flatnonzero(ok)
            if not len(pos):
                continue
            dt = c[pos + 1] - c[pos]
            L = p["dist"][pos + 1] - p["dist"][pos]
            spd = L / np.maximum(dt, 1e-3)
            m = (dt > 0) & (spd >= config.MIN_SEG_SPEED_MS) & (spd <= config.MAX_SEG_SPEED_MS)
            pos, dt = pos[m], dt[m]
            if not len(pos):
                continue
            run_id += 1
            k = len(pos)
            recs["run"].append(np.full(k, run_id, np.int64))
            recs["pattern"].append(np.full(k, pid, np.int32))
            recs["pos"].append(pos.astype(np.int32))
            recs["seg"].append(p["segs"][pos].astype(np.int32))
            recs["t"].append(c[pos])
            recs["dt"].append(dt.astype(np.float32))
    if not recs["run"]:
        return _empty()
    return pd.DataFrame({k: np.concatenate(vs) for k, vs in recs.items()})


def _empty():
    return pd.DataFrame({"run": pd.Series(dtype=np.int64), "pattern": pd.Series(dtype=np.int32),
                         "pos": pd.Series(dtype=np.int32), "seg": pd.Series(dtype=np.int32),
                         "t": pd.Series(dtype=float), "dt": pd.Series(dtype=np.float32)})


def trip_pattern_map(net):
    out = {}
    for pid, p in enumerate(net.patterns):
        for tid in p["trip_ids"]:
            out[tid] = pid
    return out
