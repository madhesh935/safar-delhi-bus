"""Step 1 - Build the transit network (stops, route patterns, geometry, segments) from static GTFS.

    python scripts/prepare_network.py [--gtfs PATH]
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
from transit.gtfs import Network  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gtfs", default=str(config.GTFS_DIR))
    a = ap.parse_args()
    config.ARTIFACTS.mkdir(parents=True, exist_ok=True)
    t = time.time()
    print(f"Reading GTFS from {a.gtfs}")
    net = Network.from_gtfs(a.gtfs)
    net.save(config.ARTIFACTS / "network.pkl")
    print(f"Saved {config.ARTIFACTS / 'network.pkl'} in {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
