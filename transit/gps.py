"""Reading and cleaning the historical GPS SQLite files (one DB per day)."""
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config

ALIASES = {
    "vehicle": ["vehicle_id", "vehicleid", "vehicle", "bus_id", "vehicle_no", "veh_id"],
    "lat": ["lat", "latitude"],
    "lon": ["lng", "lon", "long", "longitude"],
    "ts": ["timestamp", "ts", "time_stamp", "epoch", "unix_time"],
    "route": ["route_id", "routeid", "route"],
    "trip": ["trip_id", "tripid"],
    "speed": ["speed"],
}
DAY_RE = re.compile(r"(\d{4})[_-](\d{2})[_-](\d{2})")


def list_day_files(gps_dir):
    """{date: path} for every bus_movements_YYYY_MM_DD.db under gps_dir (recursive)."""
    out = {}
    for p in Path(gps_dir).rglob("*.db"):
        m = DAY_RE.search(p.name)
        if m:
            out[date(int(m[1]), int(m[2]), int(m[3]))] = p
    return dict(sorted(out.items()))


def parse_days(spec, available):
    """'all' | '2026-10-01:2026-10-24' | '2026-10-01,2026-10-05'  -> list of dates present."""
    if spec in (None, "", "all"):
        return list(available)
    days = []
    for part in spec.split(","):
        if ":" in part:
            a, b = (datetime.strptime(x.strip(), "%Y-%m-%d").date() for x in part.split(":"))
            d = a
            while d <= b:
                days.append(d)
                d += timedelta(days=1)
        else:
            days.append(datetime.strptime(part.strip(), "%Y-%m-%d").date())
    return [d for d in days if d in available]


def daytype(d) -> int:
    """0 = weekday, 1 = Saturday, 2 = Sunday / holiday."""
    if isinstance(d, datetime):
        d = d.date()
    if d.isoformat() in config.HOLIDAYS or d.weekday() == 6:
        return 2
    return 1 if d.weekday() == 5 else 0


def ist_midnight_epoch(d) -> int:
    """Unix time of 00:00 IST on date d."""
    return int((datetime(d.year, d.month, d.day) - datetime(1970, 1, 1)).total_seconds()) - config.IST_OFFSET_S


def detect_schema(con):
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
        low = {c.lower(): c for c in cols}
        found = {}
        for k, names in ALIASES.items():
            for n in names:
                if n in low:
                    found[k] = low[n]
                    break
        if all(k in found for k in ("vehicle", "lat", "lon", "ts")):
            return t, found
    raise RuntimeError(f"No GPS table with vehicle/lat/lon/timestamp columns. Tables: {tables}")


def iter_day(path, batch_vehicles=400, chunk_rows=2_000_000):
    """Yield DataFrames [vehicle, lat, lon, ts, route, trip] in batches of whole vehicles.

    The day is read with ONE sequential scan (no index needed; a 4 GB file reads in ~1-2 min),
    slimmed to compact dtypes, sorted by vehicle/time in memory, then handed out in vehicle batches."""
    con = sqlite3.connect(str(path))
    try:
        table, cols = detect_schema(con)
        sel = ", ".join(f'"{cols[k]}" AS {k}' for k in ("vehicle", "lat", "lon", "ts", "route") if k in cols)
        parts = []
        for df in pd.read_sql_query(f'SELECT {sel} FROM "{table}"', con, chunksize=chunk_rows):
            df = normalise(df)
            parts.append(pd.DataFrame({
                "vehicle": df["vehicle"].astype("category"),
                "lat": df["lat"].astype(np.float32), "lon": df["lon"].astype(np.float32),
                "ts": df["ts"].astype(np.int64), "route": df["route"].astype("category")}))
    finally:
        con.close()
    if not parts:
        return
    day = pd.concat(parts, ignore_index=True)
    del parts
    for c in ("vehicle", "route"):                       # shared string objects -> compact categories
        day[c] = day[c].astype("category")
    day = day.sort_values(["vehicle", "ts"], kind="stable").reset_index(drop=True)
    day["trip"] = ""
    v = day["vehicle"].cat.codes.to_numpy()
    starts = np.flatnonzero(np.r_[True, v[1:] != v[:-1]])
    for i in range(0, len(starts), batch_vehicles):
        a = starts[i]
        b = starts[i + batch_vehicles] if i + batch_vehicles < len(starts) else len(day)
        out = day.iloc[a:b].copy()
        out["vehicle"] = out["vehicle"].astype(str)
        out["route"] = out["route"].astype(str)
        yield out


def read_window(path, ts_from, ts_to):
    """All points with ts in [ts_from, ts_to) (used by the live replay)."""
    con = sqlite3.connect(str(path))
    try:
        table, cols = detect_schema(con)
        try:
            con.execute(f'CREATE INDEX IF NOT EXISTS idx_ts ON "{table}"("{cols["ts"]}")')
            con.commit()
        except sqlite3.Error:
            pass
        sel = ", ".join(f'"{cols[k]}" AS {k}' for k in ("vehicle", "lat", "lon", "ts", "route", "trip") if k in cols)
        df = pd.read_sql_query(f'SELECT {sel} FROM "{table}" WHERE "{cols["ts"]}" >= ? AND "{cols["ts"]}" < ?',
                               con, params=(int(ts_from), int(ts_to)))
        return normalise(df)
    finally:
        con.close()


def normalise(df):
    for k in ("route", "trip"):
        if k not in df:
            df[k] = ""
    df["vehicle"] = df["vehicle"].astype(str)
    df["route"] = df["route"].fillna("").astype(str).str.strip()
    df["trip"] = df["trip"].fillna("").astype(str).str.strip()
    df["route"] = df["route"].str.replace(r"\.0$", "", regex=True)
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    ts = pd.to_numeric(df["ts"], errors="coerce")
    if ts.notna().any() and ts.median() > 1e11:      # milliseconds -> seconds
        ts = ts / 1000.0
    df["ts"] = ts
    return df.dropna(subset=["lat", "lon", "ts"])


def clean(df):
    """Remove out-of-area points, duplicates, GPS spikes; down-sample; sort."""
    la0, lo0, la1, lo1 = config.DELHI_BBOX
    df = df[(df.lat > la0) & (df.lat < la1) & (df.lon > lo0) & (df.lon < lo1)]
    df = df.sort_values(["vehicle", "ts"], kind="stable")
    df = df.drop_duplicates(["vehicle", "ts"])
    # down-sample per vehicle: keep a point only if >= spacing after the previous kept one (approx. via bucketing)
    bucket = (df["ts"] // config.MIN_POINT_SPACING_S).astype(np.int64)
    df = df.loc[~pd.DataFrame({"v": df["vehicle"].to_numpy(), "b": bucket.to_numpy()}, index=df.index).duplicated()]
    # spike removal: a point far from both neighbours at impossible speed
    from .geo import to_xy
    x, y = to_xy(df["lat"].to_numpy(), df["lon"].to_numpy())
    t = df["ts"].to_numpy(float)
    v = df["vehicle"].to_numpy()
    same_prev = np.r_[False, v[1:] == v[:-1]]
    same_next = np.r_[v[:-1] == v[1:], False]
    dprev = np.r_[np.inf, np.hypot(np.diff(x), np.diff(y))]
    tprev = np.r_[1.0, np.maximum(np.diff(t), 1.0)]
    sp_prev = np.where(same_prev, dprev / tprev, 0)
    sp_next = np.r_[sp_prev[1:], 0]
    spike = (sp_prev > config.MAX_JUMP_SPEED_MS) & ((sp_next > config.MAX_JUMP_SPEED_MS) | ~same_next)
    df = df.loc[~spike].copy()
    df["x"], df["y"] = to_xy(df["lat"].to_numpy(), df["lon"].to_numpy())
    return df
