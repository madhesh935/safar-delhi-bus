"""Step 3 - Train the travel-time model from matched traversals.

    python scripts/build_model.py --train all
    python scripts/build_model.py --train 2026-10-01:2026-10-24
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd  # noqa: E402

import config  # noqa: E402
from transit import gps  # noqa: E402
from transit.gtfs import Network  # noqa: E402
from transit.model import TravelTimeModel  # noqa: E402


def load_traversals(spec):
    folder = config.ARTIFACTS / "traversals"
    avail = {}
    for p in sorted(folder.glob("*.pkl")):
        from datetime import date
        avail[date.fromisoformat(p.stem)] = p
    days = gps.parse_days(spec, avail)
    if not days:
        sys.exit(f"No traversal files for '{spec}' in {folder}. Run scripts/match_gps.py first.")
    dfs = [pd.read_pickle(avail[d]) for d in days]
    dfs = [d for d in dfs if len(d)]
    return pd.concat(dfs, ignore_index=True), days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", default="all")
    ap.add_argument("--out", default=str(config.ARTIFACTS / "model.npz"))
    a = ap.parse_args()
    net = Network.load(config.ARTIFACTS / "network.pkl")
    trav, days = load_traversals(a.train)
    print(f"Training on {len(days)} day(s): {days[0]} .. {days[-1]}")
    model = TravelTimeModel.build(trav, net)
    model.save(a.out)
    print(f"Saved {a.out}")


if __name__ == "__main__":
    main()
