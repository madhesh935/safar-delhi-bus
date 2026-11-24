"""
Central configuration. Edit the two paths below (or set the env vars GPS_DIR / GTFS_DIR).

Windows example:
    GPS_DIR  = r"C:\\Users\\Madhesh\\Downloads\\delhi-dtc-bus-gtfs-rt-2026-10"
    GTFS_DIR = r"C:\\Users\\Madhesh\\Downloads\\GTFS"
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------- data paths
GPS_DIR = Path(os.getenv("GPS_DIR", r"C:\Users\YOU\Downloads\delhi-dtc-bus-gtfs-rt-2026-10"))
GTFS_DIR = Path(os.getenv("GTFS_DIR", r"C:\Users\YOU\Downloads\GTFS"))
ARTIFACTS = Path(os.getenv("ARTIFACTS_DIR", ROOT / "artifacts"))
DOCS = ROOT / "docs"

# ---------------------------------------------------------------- calendar
# Gazetted holidays inside the data window are treated like Sundays.
# (Verify against the Delhi govt holiday list.)
HOLIDAYS = {"2026-10-02", "2026-10-20"}
IST_OFFSET_S = 19800                       # UTC+05:30

# ---------------------------------------------------------------- GPS cleaning
DELHI_BBOX = (28.20, 76.70, 29.05, 77.65)  # lat_min, lon_min, lat_max, lon_max
MIN_POINT_SPACING_S = 10                   # down-sample: keep >= 1 point / 10 s
MAX_JUMP_SPEED_MS = 25.0                   # ~90 km/h: faster jumps are GPS spikes

# ---------------------------------------------------------------- map matching
MAX_OFFSET_SHAPE_M = 70                    # max distance from route shape
MAX_OFFSET_STOPLINE_M = 220                # when only stop-to-stop polyline exists
MAX_GAP_S = 300                            # break a run if no GPS for 5 min
MAX_BRACKET_GAP_S = 180                    # max gap around a stop to interpolate crossing
MIN_SEG_SPEED_MS = 0.4                     # 1.4 km/h  (slower = bus parked / out of service)
MAX_SEG_SPEED_MS = 22.0                    # 80 km/h

# ---------------------------------------------------------------- model
BIN_S = 1800                               # 30-minute time-of-day bins
N_BINS = 86400 // BIN_S
SHRINK_K = 4.0                             # Bayesian shrinkage strength (pseudo-observations)
TRIP_CORRELATION = 0.6                     # correlation of segment delays inside one trip

# ---------------------------------------------------------------- journey planner
WALK_SPEED_MS = 1.25                       # 4.5 km/h
WALK_DETOUR = 1.25                         # street network vs straight line
ACCESS_RADIUS_M = 800                      # max walk to first / from last stop
TRANSFER_RADIUS_M = 250                    # max walk between stops when changing bus
TRANSFER_SLACK_S = 60                      # min time to change bus
MAX_TRANSFERS = 2

# ---------------------------------------------------------------- live nowcast
# LIVE_MODE = off | replay | gtfsrt
#   replay  : re-plays a historical day as if it were live (great for demos/testing)
#   gtfsrt  : polls the live GTFS-RT VehiclePositions feed of the Delhi Transport Stack
LIVE_MODE = os.getenv("LIVE_MODE", "off")
LIVE_WINDOW_S = 1200                       # use last 20 min of live data
LIVE_DECAY_S = 1800                        # live effect fades over 30 min into the future
LIVE_REPLAY_DB = os.getenv("LIVE_REPLAY_DB", "")             # path to a bus_movements_*.db
LIVE_REPLAY_START = os.getenv("LIVE_REPLAY_START", "2026-10-28T08:00")  # IST
GTFSRT_URL = os.getenv("GTFSRT_URL", "")
GTFSRT_API_KEY = os.getenv("GTFSRT_API_KEY", "")
