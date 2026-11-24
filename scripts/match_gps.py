"""Step 2 - Map-match historical GPS to route patterns and extract stop-to-stop traversals.

    python scripts/match_gps.py --days all --workers 4
    python scripts/match_gps.py --days 2026-10-01:2026-10-07

One output file per day: artifacts/traversals/YYYY-MM-DD.pkl  (re-runs skip finished days).
"""
import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd  # noqa: E402

import config  # noqa: E402
from transit import gps  # noqa: E402
from transit.gtfs import Network  # noqa: E402
from transit.matcher import match_points, trip_pattern_map  # noqa: E402

_NET = None
_T2P = None


def _init():
    global _NET, _T2P
    _NET = Network.load(config.ARTIFACTS / "network.pkl")
    _T2P = trip_pattern_map(_NET)


def process_day(day, path, max_vehicles=None):
    if _NET is None:
        _init()
    t = time.time()
    dstart = gps.ist_midnight_epoch(day)
    parts, n_pts, n_veh, offset = [], 0, 0, 0
    for batch in gps.iter_day(path):
        n_pts += len(batch)
        n_veh += batch["vehicle"].nunique()
        b = gps.clean(batch)
        tr = match_points(b, _NET, dstart, run_offset=offset, trip_to_pattern=_T2P)
        if len(tr):
            offset = int(tr["run"].max())
            parts.append(tr)
        if max_vehicles and n_veh >= max_vehicles:
            break
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if len(out):
        out["date"] = day.isoformat()
        out["dtype"] = gps.daytype(day)
    dest = config.ARTIFACTS / "traversals" / f"{day.isoformat()}.pkl"
    out.to_pickle(dest)
    return day, n_pts, n_veh, len(out), (out["run"].nunique() if len(out) else 0), time.time() - t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gps", default=str(config.GPS_DIR))
    ap.add_argument("--days", default="all", help="all | 2026-10-01:2026-10-24 | comma list")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--max-vehicles", type=int, default=None, help="quick test: stop after N vehicles/day")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    files = gps.list_day_files(a.gps)
    if not files:
        sys.exit(f"No bus_movements_YYYY_MM_DD.db files found under {a.gps}")
    days = gps.parse_days(a.days, files)
    (config.ARTIFACTS / "traversals").mkdir(parents=True, exist_ok=True)
    todo = [d for d in days if a.force or not (config.ARTIFACTS / "traversals" / f"{d}.pkl").exists()]
    print(f"{len(files)} day files found; processing {len(todo)} day(s) with {a.workers} worker(s)")

    def report(r):
        d, npts, nveh, ntr, nruns, sec = r
        print(f"  {d}: {npts:,} GPS points, {nveh:,} vehicles -> {nruns:,} matched runs, "
              f"{ntr:,} stop-to-stop traversals  ({sec:.0f}s)")

    if a.workers <= 1:
        _init()
        for d in todo:
            report(process_day(d, files[d], a.max_vehicles))
    else:
        with ProcessPoolExecutor(max_workers=a.workers, initializer=_init) as ex:
            futs = [ex.submit(process_day, d, files[d], a.max_vehicles) for d in todo]
            for f in as_completed(futs):
                report(f.result())


if __name__ == "__main__":
    main()
