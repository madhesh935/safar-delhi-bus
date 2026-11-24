"""Generate a small synthetic Delhi-like GTFS + GPS dataset to smoke-test the whole pipeline
(no real data needed). Uses the same file formats as the hackathon data.

    python scripts/make_synthetic.py            # writes demo_data/gtfs and demo_data/gps
    set GTFS_DIR=demo_data\\gtfs & set GPS_DIR=demo_data\\gps   (Windows)  then run the pipeline
"""
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo_data"
rng = np.random.default_rng(42)


def line(lat0, lon0, lat1, lon1, n):
    return np.linspace(lat0, lat1, n), np.linspace(lon0, lon1, n)


def congestion(h, dtype):
    """True slowdown factor (>1 = slower) by hour."""
    am = np.exp(-((h - 9.2) / 1.3) ** 2)
    pm = np.exp(-((h - 18.6) / 1.6) ** 2)
    amp = {0: 1.0, 1: 0.55, 2: 0.3}[dtype]
    return 1.0 + amp * (1.1 * am + 1.3 * pm)


def main():
    (OUT / "gtfs").mkdir(parents=True, exist_ok=True)
    (OUT / "gps").mkdir(parents=True, exist_ok=True)
    # --- three corridors; B and C share stops with A
    la, lo = line(28.6675, 77.2280, 28.5490, 77.2510, 22)          # Kashmere Gate -> Nehru Place
    lb, lob = line(28.6300, 77.1000, 28.6200, 77.3000, 20)         # west-east
    names_a = ["Kashmere Gate ISBT"] + [f"A Stop {i}" for i in range(1, 21)] + ["Nehru Place"]
    stops = [(f"A{i}", names_a[i], la[i], lo[i]) for i in range(22)]
    stops += [(f"B{i}", f"B Stop {i}" if i not in (0, 19) else ("Punjabi Bagh" if i == 0 else "Anand Vihar ISBT"),
               lb[i], lob[i]) for i in range(20)]
    stop_df = pd.DataFrame(stops, columns=["stop_id", "stop_name", "stop_lat", "stop_lon"])
    stop_df.to_csv(OUT / "gtfs" / "stops.txt", index=False)
    seqs = {
        "534_UP": ("534", [f"A{i}" for i in range(22)]),
        "534_DN": ("534", [f"A{i}" for i in reversed(range(22))]),
        "419_UP": ("419", [f"B{i}" for i in range(20)]),
        "419_DN": ("419", [f"B{i}" for i in reversed(range(20))]),
        "181_UP": ("181", [f"B{i}" for i in range(0, 9)] + [f"A{i}" for i in range(9, 22)]),
        "181_DN": ("181", [f"A{i}" for i in reversed(range(9, 22))] + [f"B{i}" for i in reversed(range(0, 9))]),
    }
    pd.DataFrame({"route_id": ["534", "419", "181"], "route_short_name": ["534", "419", "181"],
                  "route_long_name": ["Kashmere Gate - Nehru Place", "Punjabi Bagh - Anand Vihar",
                                      "Punjabi Bagh - Nehru Place"], "route_type": 3}) \
        .to_csv(OUT / "gtfs" / "routes.txt", index=False)
    pd.DataFrame({"service_id": ["ALL"], **{d: [1] for d in ("monday", "tuesday", "wednesday", "thursday",
                                                             "friday", "saturday", "sunday")},
                  "start_date": ["20260101"], "end_date": ["20271231"]}).to_csv(OUT / "gtfs" / "calendar.txt", index=False)
    S = stop_df.set_index("stop_id")
    seg_slow = {}
    trips, st, shapes = [], [], []
    for sid, (rid, seq) in seqs.items():
        lat = S.loc[seq, "stop_lat"].to_numpy()
        lon = S.loc[seq, "stop_lon"].to_numpy()
        # shape with midpoints
        for k in range(len(seq)):
            shapes.append((sid, lat[k], lon[k], 2 * k))
            if k < len(seq) - 1:
                shapes.append((sid, (lat[k] + lat[k + 1]) / 2 + 0.0002, (lon[k] + lon[k + 1]) / 2, 2 * k + 1))
        for dep in range(5 * 3600 + 1800, 22 * 3600 + 1800, 600):
            tid = f"{sid}_{dep}"
            trips.append((rid, "ALL", tid, sid))
            t = dep
            for k, s in enumerate(seq):
                st.append((tid, f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}", s, k + 1))
                t += 150                                         # timetable: fixed 2.5 min per stop
        for k in range(len(seq) - 1):
            seg_slow.setdefault((seq[k], seq[k + 1]), rng.uniform(0.8, 1.6))
    pd.DataFrame(trips, columns=["route_id", "service_id", "trip_id", "shape_id"]).to_csv(OUT / "gtfs" / "trips.txt", index=False)
    stdf = pd.DataFrame(st, columns=["trip_id", "arrival_time", "stop_id", "stop_sequence"])
    stdf["departure_time"] = stdf["arrival_time"]
    stdf.to_csv(OUT / "gtfs" / "stop_times.txt", index=False)
    pd.DataFrame(shapes, columns=["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"]) \
        .to_csv(OUT / "gtfs" / "shapes.txt", index=False)

    # --- GPS days
    from transit.geo import haversine
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(10)]
    for d in days:
        dtype = 2 if d.weekday() == 6 or d.isoformat() == "2026-10-02" else (1 if d.weekday() == 5 else 0)
        day_factor = rng.normal(1.0, 0.06)
        base = int((pd.Timestamp(d) - pd.Timestamp("1970-01-01")).total_seconds()) - 19800
        rows = []
        for sid, (rid, seq) in seqs.items():
            lat = S.loc[seq, "stop_lat"].to_numpy()
            lon = S.loc[seq, "stop_lon"].to_numpy()
            seglen = haversine(lat[:-1], lon[:-1], lat[1:], lon[1:])
            for n, dep in enumerate(range(5 * 3600 + 1800, 22 * 3600 + 1800, 600)):
                veh = f"DL1PD{abs(hash(sid)) % 900 + 100}{n:03d}"
                t = dep + rng.normal(0, 90)
                trip_noise = rng.lognormal(0, 0.10)
                tp, xp, yp = [t], [lat[0]], [lon[0]]
                for k in range(len(seq) - 1):
                    v = 7.5 / (congestion(t / 3600, dtype) * seg_slow[(seq[k], seq[k + 1])] * trip_noise * day_factor)
                    dt = seglen[k] / v + 20 + rng.exponential(10)      # running + dwell
                    t += dt
                    tp.append(t); xp.append(lat[k + 1]); yp.append(lon[k + 1])
                ts = np.arange(tp[0], tp[-1], 10.0)
                la_ = np.interp(ts, tp, xp) + rng.normal(0, 0.00006, len(ts))
                lo_ = np.interp(ts, tp, yp) + rng.normal(0, 0.00006, len(ts))
                spike = rng.random(len(ts)) < 0.01
                la_[spike] += 0.006
                keep = rng.random(len(ts)) > 0.05                    # 5% dropped packets
                for a, b, c in zip(ts[keep], la_[keep], lo_[keep]):
                    rows.append((veh, float(b), float(c), 0.0, rid, "", int(base + a)))
        df = pd.DataFrame(rows, columns=["vehicle_id", "lat", "lng", "speed", "route_id", "trip_id", "timestamp"])
        df["date"] = d.isoformat()
        df["time"] = pd.to_datetime(df["timestamp"] + 19800, unit="s").dt.strftime("%H:%M:%S")
        p = OUT / "gps" / f"bus_movements_{d.year}_{d.month:02d}_{d.day:02d}.db"
        p.unlink(missing_ok=True)
        with sqlite3.connect(p) as con:
            df.to_sql("bus_movements", con, index=False)
        print(f"  {p.name}: {len(df):,} points")
    print(f"Synthetic data in {OUT}")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
