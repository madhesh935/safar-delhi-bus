"""
Travel-time model.

For every road segment (stop A -> stop B, pooled across all routes that use it), day type
(weekday / Saturday / Sunday+holiday) and 30-minute time bin we estimate the median travel time
and its spread, in log space:

    log t[seg, day, bin] = shrink( observed median ,  prior = base[seg] + F[day, bin] )

  * base[seg]   : the segment's all-day median (or, if never observed, a fallback from its length
                  and its scheduled time, calibrated on observed segments)
  * F[day, bin] : the city-wide congestion profile learned from the whole fleet
  * shrink      : empirical-Bayes weighting n/(n+k) - sparse cells lean on the prior,
                  well-observed cells trust the data. Residuals are smoothed over neighbouring bins.

Journey times are obtained by *time propagation*: each segment is looked up at the moment the bus
is predicted to enter it, so a trip starting at 8:00 that reaches the city centre at 8:50 uses the
8:30-9:00 speeds there.
"""
import json

import numpy as np
import pandas as pd

import config

B = config.N_BINS


def tbin(t):
    return (np.asarray(t, float) // config.BIN_S).astype(np.int64) % B


def _circ_fill(a):
    """Fill NaNs in a 1-D circular array by linear interpolation."""
    a = a.copy()
    ok = np.isfinite(a)
    if ok.all() or not ok.any():
        return np.where(ok, a, 0.0)
    n = len(a)
    xi = np.flatnonzero(ok)
    xp = np.r_[xi - n, xi, xi + n]
    fp = np.r_[a[xi], a[xi], a[xi]]
    a[~ok] = np.interp(np.flatnonzero(~ok), xp, fp)
    return a


def _smooth_bins(a):
    return 0.25 * np.roll(a, 1, axis=-1) + 0.5 * a + 0.25 * np.roll(a, -1, axis=-1)


class TravelTimeModel:
    # ------------------------------------------------------------------ training
    @classmethod
    def build(cls, trav: pd.DataFrame, net, k=config.SHRINK_K, verbose=True):
        """trav needs columns seg, t, dt, dtype (+ pattern, pos, date for headways)."""
        log = print if verbose else (lambda *a, **kw: None)
        self = cls()
        nseg = len(net.seg_from)
        seg = trav["seg"].to_numpy(np.int64)
        dty = trav["dtype"].to_numpy(np.int64)
        b = tbin(trav["t"].to_numpy())
        ld = np.log(trav["dt"].to_numpy(float))
        df = pd.DataFrame({"seg": seg, "d": dty, "b": b, "ld": ld})

        # base (all-day) level per observed segment
        mseg = df.groupby("seg")["ld"].median()
        has = np.zeros(nseg, bool)
        has[mseg.index.to_numpy()] = True
        m_seg = np.full(nseg, np.nan)
        m_seg[mseg.index.to_numpy()] = mseg.to_numpy()

        # city congestion profile F[day, bin]
        df["rel"] = ld - m_seg[seg]
        Fs = df.groupby(["d", "b"])["rel"].median()
        F = np.full((3, B), np.nan)
        for (d, bb), v in Fs.items():
            F[d, bb] = v
        for d in range(3):
            if not np.isfinite(F[d]).any():
                F[d] = F[0] if np.isfinite(F[0]).any() else 0.0
            F[d] = _circ_fill(F[d])

        # fallback base for never-observed segments: length (speed) + calibrated schedule
        L = np.maximum(net.seg_len, 20.0)
        v_ref = np.median(np.log(L[has]) - m_seg[has]) if has.any() else np.log(4.0)
        fb = np.log(L) - v_ref
        sched = net.seg_sched
        okS = np.isfinite(sched) & (sched >= 20)
        if (has & okS).sum() > 20:
            R = np.median(m_seg[has & okS] - np.log(sched[has & okS]))
            fb = np.where(okS, 0.5 * fb + 0.5 * (np.log(np.where(okS, sched, 1.0)) + R), fb)
        base = np.where(has, m_seg, fb)

        # cell statistics
        cell = df.groupby(["seg", "d", "b"])["ld"].agg(["count", "median", "std"])
        si, di, bi = (cell.index.get_level_values(i).to_numpy() for i in range(3))
        n = np.zeros((nseg, 3, B), np.float32)
        med = np.zeros((nseg, 3, B), np.float32)
        sd = np.full((nseg, 3, B), np.nan, np.float32)
        n[si, di, bi] = cell["count"].to_numpy()
        med[si, di, bi] = cell["median"].to_numpy()
        sd[si, di, bi] = cell["std"].to_numpy()

        prior = base[:, None, None] + F[None, :, :]
        w = n / (n + k)
        res = w * (med - prior)                      # shrunk residual (0 where no data)
        est = prior + _smooth_bins(res)
        lo = np.log(L / config.MAX_SEG_SPEED_MS)[:, None, None]
        hi = np.log(L / config.MIN_SEG_SPEED_MS)[:, None, None]
        est = np.clip(est, lo, hi)

        # spread (log-sd), shrunk towards the city-wide spread of that day/bin
        S = np.full((3, B), np.nan)
        big = n >= 5
        for d in range(3):
            for bb in range(B):
                v = sd[:, d, bb][big[:, d, bb]]
                if len(v):
                    S[d, bb] = np.median(v)
        gS = np.nanmedian(S) if np.isfinite(S).any() else 0.3
        S = np.where(np.isfinite(S), S, gS)
        sdv = np.where(np.isfinite(sd), sd, S[None])
        lsig = (n * sdv + 8.0 * S[None]) / (n + 8.0)

        # sum of segment medians under-estimates a trip (right-skewed delays): use the mean of the log-normal
        self.mu = np.exp(est + 0.5 * lsig ** 2).astype(np.float32)   # expected seconds
        self.lsig = lsig.astype(np.float32)               # log-sd
        self.cnt = np.minimum(n, 32000).astype(np.int16)
        self.F = F.astype(np.float32)
        self.has = has
        self.freeflow = self.mu.min(axis=(1, 2))
        self.base_static = np.exp(base).astype(np.float32)  # time-of-day-agnostic baseline
        self._headways(trav, net)
        self.meta = {
            "n_traversals": int(len(trav)),
            "segments_total": int(nseg),
            "segments_observed": int(has.sum()),
            "coverage_pct": round(100.0 * has.mean(), 1),
            "days": sorted({str(x) for x in trav["date"].unique()}) if "date" in trav else [],
        }
        log(f"  model: {len(trav):,} traversals, {has.sum():,}/{nseg:,} segments observed ({100*has.mean():.1f}%)")
        return self

    def _headways(self, trav, net):
        npat = len(net.patterns)
        self.EH = np.zeros((npat, 3, 24), np.float32)
        self.EH2 = np.zeros((npat, 3, 24), np.float32)
        self.nH = np.zeros((npat, 3, 24), np.int32)
        if "date" not in trav or trav.empty:
            return
        h = trav[["pattern", "pos", "date", "dtype", "t"]].sort_values(["pattern", "pos", "date", "t"])
        same = (h["pattern"].to_numpy()[1:] == h["pattern"].to_numpy()[:-1]) & \
               (h["pos"].to_numpy()[1:] == h["pos"].to_numpy()[:-1]) & \
               (h["date"].to_numpy()[1:] == h["date"].to_numpy()[:-1])
        hw = np.diff(h["t"].to_numpy())
        ok = same & (hw >= 60) & (hw <= 5400)
        sub = pd.DataFrame({
            "p": h["pattern"].to_numpy()[1:][ok], "d": h["dtype"].to_numpy()[1:][ok],
            "hr": (h["t"].to_numpy()[1:][ok] // 3600).astype(int) % 24, "h": hw[ok]})
        sub["h2"] = sub["h"] ** 2
        g = sub.groupby(["p", "d", "hr"]).agg(EH=("h", "mean"), EH2=("h2", "mean"), n=("h", "size"))
        p, d, hr = (g.index.get_level_values(i).to_numpy() for i in range(3))
        self.EH[p, d, hr] = g["EH"].to_numpy()
        self.EH2[p, d, hr] = g["EH2"].to_numpy()
        self.nH[p, d, hr] = g["n"].to_numpy()

    # ------------------------------------------------------------------ persistence
    def save(self, path):
        np.savez_compressed(path, mu=self.mu, lsig=self.lsig, cnt=self.cnt, F=self.F, has=self.has,
                            freeflow=self.freeflow, base_static=self.base_static,
                            EH=self.EH, EH2=self.EH2, nH=self.nH, meta=json.dumps(self.meta))

    @classmethod
    def load(cls, path):
        self = cls()
        z = np.load(path, allow_pickle=False)
        for k in ("mu", "lsig", "cnt", "F", "has", "freeflow", "base_static", "EH", "EH2", "nH"):
            setattr(self, k, z[k])
        self.meta = json.loads(str(z["meta"]))
        return self

    # ------------------------------------------------------------------ prediction
    def propagate(self, segs, t0, dtype, multiplier=None):
        """Times at each stop when starting at t0 and traversing segs in order (time propagation)."""
        segs = np.asarray(segs, np.int64)
        times = np.empty(len(segs) + 1)
        times[0] = t = float(t0)
        for i, s in enumerate(segs):
            dt = float(self.mu[s, dtype, int(t // config.BIN_S) % B])
            if multiplier is not None:
                dt *= multiplier(int(s), t)
            t += dt
            times[i + 1] = t
        return times

    def ride_sd(self, segs, times, dtype):
        """Std-dev (s) of a ride, combining per-segment log-sd with intra-trip correlation."""
        segs = np.asarray(segs, np.int64)
        if not len(segs):
            return 0.0
        bins = tbin(times[:-1])
        dts = np.diff(times)
        sds = dts * self.lsig[segs, dtype, bins]
        rho = config.TRIP_CORRELATION
        return float(np.sqrt((1 - rho) * np.sum(sds ** 2) + rho * np.sum(sds) ** 2))

    def observed_frequency(self, pattern, dtype, t):
        """(mean headway s, expected wait s) from observed bus passages, or (None, None)."""
        hr = int(t // 3600) % 24
        n = self.nH[pattern, dtype, hr]
        if n < 4:
            return None, None
        eh, eh2 = float(self.EH[pattern, dtype, hr]), float(self.EH2[pattern, dtype, hr])
        return eh, eh2 / (2 * eh)          # waiting-time paradox: E[W] = E[H^2] / 2E[H]

    def quiet_time(self, dtype):
        """Middle of the least congested 30-min bin between 06:00 and 22:00."""
        lo, hi = 6 * 3600 // config.BIN_S, 22 * 3600 // config.BIN_S
        b = lo + int(np.argmin(self.F[dtype, lo:hi]))
        return b * config.BIN_S + config.BIN_S // 2
