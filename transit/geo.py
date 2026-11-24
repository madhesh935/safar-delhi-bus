"""Geometry helpers: local planar projection, polyline projection, grid spatial index."""
import numpy as np

R_EARTH = 6371000.0
LAT0, LON0 = 28.6139, 77.2090          # Delhi reference point
_KX = np.radians(1.0) * R_EARTH * np.cos(np.radians(LAT0))
_KY = np.radians(1.0) * R_EARTH


def to_xy(lat, lon):
    """Equirectangular projection around Delhi (metres). Error < 0.3% over NCR."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    return (lon - LON0) * _KX, (lat - LAT0) * _KY


def to_latlon(x, y):
    return np.asarray(y) / _KY + LAT0, np.asarray(x) / _KX + LON0


def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * R_EARTH * np.arcsin(np.sqrt(a))


class Polyline:
    """A route geometry in planar metres, supporting projection of points onto it."""

    def __init__(self, x, y):
        x = np.asarray(x, float)
        y = np.asarray(y, float)
        keep = np.ones(len(x), bool)
        keep[1:] = (np.diff(x) != 0) | (np.diff(y) != 0)
        self.x, self.y = x[keep], y[keep]
        if len(self.x) < 2:                       # degenerate -> tiny segment
            self.x = np.r_[self.x[:1], self.x[:1] + 1.0]
            self.y = np.r_[self.y[:1], self.y[:1]]
        self.ax, self.ay = self.x[:-1], self.y[:-1]
        self.dx, self.dy = np.diff(self.x), np.diff(self.y)
        self.seglen = np.hypot(self.dx, self.dy)
        self.l2 = np.maximum(self.seglen ** 2, 1e-9)
        self.cum = np.r_[0.0, np.cumsum(self.seglen)]
        self.length = float(self.cum[-1])

    def _cand(self, px, py):
        t = ((px[:, None] - self.ax) * self.dx + (py[:, None] - self.ay) * self.dy) / self.l2
        np.clip(t, 0.0, 1.0, out=t)
        cx = self.ax + t * self.dx
        cy = self.ay + t * self.dy
        d2 = (px[:, None] - cx) ** 2 + (py[:, None] - cy) ** 2
        return t, d2

    def project(self, px, py, max_cells=3_000_000):
        """Nearest-point projection. Returns (s = distance along line, d = lateral offset)."""
        px = np.asarray(px, float)
        py = np.asarray(py, float)
        n = len(px)
        s = np.empty(n)
        d = np.empty(n)
        step = max(1, max_cells // max(1, len(self.ax)))
        for a in range(0, n, step):
            b = min(n, a + step)
            t, d2 = self._cand(px[a:b], py[a:b])
            j = np.argmin(d2, axis=1)
            r = np.arange(b - a)
            s[a:b] = self.cum[j] + t[r, j] * self.seglen[j]
            d[a:b] = np.sqrt(d2[r, j])
        return s, d

    def project_sequential(self, px, py, tol=30.0):
        """Project an ordered list of points (stops) so that distances never go backwards.
        Handles loops: among candidates within `tol` m of the best, pick the earliest ahead."""
        px = np.asarray(px, float)
        py = np.asarray(py, float)
        t, d2 = self._cand(px, py)
        sall = self.cum[:-1][None, :] + t * self.seglen[None, :]
        out_s = np.empty(len(px))
        out_d = np.empty(len(px))
        prev = -1.0
        for i in range(len(px)):
            ok = sall[i] >= prev - 1.0
            if not ok.any():
                ok[:] = True
            dd = np.where(ok, np.sqrt(d2[i]), np.inf)
            best = dd.min()
            cand = np.where(dd <= best + tol)[0]
            j = cand[np.argmin(sall[i, cand])]
            out_s[i] = max(sall[i, j], prev)
            out_d[i] = np.sqrt(d2[i, j])
            prev = out_s[i]
        return out_s, out_d

    def slice_xy(self, s0, s1):
        """Coordinates of the line between distances s0 and s1 (for drawing)."""
        inner = (self.cum > s0) & (self.cum < s1)
        xs = np.r_[np.interp(s0, self.cum, self.x), self.x[inner], np.interp(s1, self.cum, self.x)]
        ys = np.r_[np.interp(s0, self.cum, self.y), self.y[inner], np.interp(s1, self.cum, self.y)]
        return xs, ys


class GridIndex:
    """Simple uniform-grid spatial index for radius queries (no scipy needed)."""

    def __init__(self, x, y, cell=250.0):
        self.x = np.asarray(x, float)
        self.y = np.asarray(y, float)
        self.cell = cell
        self.cells = {}
        cx = np.floor(self.x / cell).astype(int)
        cy = np.floor(self.y / cell).astype(int)
        for i, key in enumerate(zip(cx.tolist(), cy.tolist())):
            self.cells.setdefault(key, []).append(i)

    def query(self, x, y, radius):
        r = int(np.ceil(radius / self.cell))
        cx, cy = int(np.floor(x / self.cell)), int(np.floor(y / self.cell))
        idx = []
        for i in range(cx - r, cx + r + 1):
            for j in range(cy - r, cy + r + 1):
                idx.extend(self.cells.get((i, j), ()))
        if not idx:
            return np.array([], int), np.array([])
        idx = np.array(idx)
        d = np.hypot(self.x[idx] - x, self.y[idx] - y)
        m = d <= radius
        idx, d = idx[m], d[m]
        o = np.argsort(d)
        return idx[o], d[o]
