"""
GPS-calibrated timetable + RAPTOR journey search.

The published timetable assumes fixed running times. We keep each scheduled trip's *departure*
from its first stop, but re-predict every downstream stop time with the travel-time model
(time-propagated, per day type). RAPTOR (Delling et al., Microsoft Research 2012 - the round-based
algorithm used by production journey planners) then finds Pareto-optimal journeys
(earliest arrival for 0, 1, 2 transfers) on that predicted timetable.
"""
import numpy as np

import config
from .model import B

INF = float("inf")
FLAGS = ("wk", "sat", "sun")


class Timetables:
    def __init__(self, net, model):
        self.net, self.model = net, model
        self._cache = {}

    def get(self, dtype):
        if dtype not in self._cache:
            self._cache[dtype] = self._build(dtype)
        return self._cache[dtype]

    def _build(self, dtype):
        net, mu = self.net, self.model.mu
        flag = FLAGS[dtype]
        any_service = any(p[flag].any() for p in net.patterns)
        pred, sched, rows = [], [], []
        for p in net.patterns:
            m = p[flag] if any_service else np.ones(len(p["sched"]), bool)
            S = p["sched"][m]
            A = np.empty_like(S)
            if len(S):
                t = S[:, 0].astype(float).copy()
                A[:, 0] = t
                for i, s in enumerate(p["segs"]):
                    t = t + mu[s, dtype, (t // config.BIN_S).astype(np.int64) % B]
                    A[:, i + 1] = t
                A = np.maximum.accumulate(A, axis=0)          # FIFO: no overtaking within a pattern
            pred.append(A)
            sched.append(S)
            rows.append(np.flatnonzero(m))
        return {"pred": pred, "sched": sched, "rows": rows}


class Raptor:
    def __init__(self, net):
        self.net = net
        self.pat_stops = [p["stops"] for p in net.patterns]
        # footpaths between nearby stops (for transfers)
        x, y = net.stops["x"].to_numpy(), net.stops["y"].to_numpy()
        self.transfers = []
        for s in range(len(x)):
            idx, d = net.grid.query(x[s], y[s], config.TRANSFER_RADIUS_M)
            self.transfers.append([(int(j), float(dd * config.WALK_DETOUR / config.WALK_SPEED_MS + 30))
                                   for j, dd in zip(idx, d) if j != s])

    def query(self, tt, origin, target, max_transfers=config.MAX_TRANSFERS, banned=frozenset()):
        """origin: {stop: earliest time at stop}; target: {stop: walk seconds to destination}.
        Returns list of journeys (one per #transfers that improves arrival)."""
        net = self.net
        K = max_transfers + 1
        tau = [dict() for _ in range(K + 1)]
        parent = [dict() for _ in range(K + 1)]
        best = {}
        for s, t in origin.items():
            tau[0][s] = t
            best[s] = t
            parent[0][s] = ("origin",)
        marked = set(origin)
        target_best = INF
        found = []
        pred = tt["pred"]
        for k in range(1, K + 1):
            Q = {}
            for s in marked:
                for p, pos in net.stop_patterns[s]:
                    if p in banned:
                        continue
                    if p not in Q or pos < Q[p]:
                        Q[p] = pos
            marked = set()
            prev, cur, par = tau[k - 1], tau[k], parent[k]
            slack = config.TRANSFER_SLACK_S if k > 1 else 0
            for p, pos0 in Q.items():
                A = pred[p]
                if not len(A):
                    continue
                stops = self.pat_stops[p]
                trip, board = -1, -1
                for i in range(pos0, len(stops)):
                    s = int(stops[i])
                    if trip >= 0:
                        a = A[trip, i]
                        if a < best.get(s, INF) and a < target_best:
                            cur[s] = a
                            best[s] = a
                            par[s] = ("ride", p, trip, board, i)
                            marked.add(s)
                    ps = prev.get(s)
                    if ps is not None and i < len(stops) - 1:
                        need = ps + slack
                        if trip < 0 or need <= A[trip, i]:
                            j = int(np.searchsorted(A[:, i], need, side="left"))
                            if j < len(A) and (trip < 0 or j < trip):
                                trip, board = j, i
            for s in list(marked):
                t = cur[s]
                for s2, w in self.transfers[s]:
                    if t + w < best.get(s2, INF) and t + w < target_best:
                        cur[s2] = t + w
                        best[s2] = t + w
                        par[s2] = ("walk", s, w)
                        marked.add(s2)
            bt, bs = INF, None
            for s, w in target.items():
                if s in cur and cur[s] + w < bt:
                    bt, bs = cur[s] + w, s
            if bs is not None and bt < target_best:
                target_best = bt
                found.append(self._reconstruct(parent, k, bs, bt))
            if not marked:
                break
        return [j for j in found if j is not None]

    def _reconstruct(self, parent, k, s, arrival):
        legs = []
        r = k
        last = s
        for _ in range(4 * (k + 2)):
            pr = parent[r].get(s)
            if pr is None:
                return None
            if pr[0] == "origin":
                legs.reverse()
                if not any(l["type"] == "bus" for l in legs):
                    return None
                return {"rounds": k, "last_stop": last, "arrival": arrival, "legs": legs, "first_stop": s}
            if pr[0] == "walk":
                legs.append({"type": "transfer_walk", "from": pr[1], "to": s, "sec": pr[2]})
                s = pr[1]
            else:
                _, p, trip, board, alight = pr
                legs.append({"type": "bus", "pattern": p, "trip": trip, "board": board, "alight": alight})
                s = int(self.pat_stops[p][board])
                r -= 1
        return None
