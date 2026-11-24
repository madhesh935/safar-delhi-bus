"""
Live nowcast: compares how fast buses moved in the last 20 minutes with what the historical model
expected, per segment, and nudges near-term predictions accordingly (the effect fades over ~30 min).

Sources
  replay : re-plays a historical day from a bus_movements_*.db as if it were live (demo / testing)
  gtfsrt : polls the Delhi Transport Stack GTFS-RT VehiclePositions feed
"""
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config
from . import gps
from .matcher import match_points, trip_pattern_map
from .model import tbin


class LiveNowcaster:
    def __init__(self, net, model, mode=config.LIVE_MODE):
        self.net, self.model, self.mode = net, model, mode
        self.factors, self.counts = {}, {}
        self.city_log_ratio = 0.0
        self.n_vehicles = 0
        self.n_traversals = 0
        self._last = 0.0
        self._lock = threading.Lock()
        self._t2p = trip_pattern_map(net)
        self._buffer = []
        if mode == "replay":
            if not config.LIVE_REPLAY_DB:
                raise ValueError("LIVE_MODE=replay needs LIVE_REPLAY_DB=<path to bus_movements_*.db>")
            start = datetime.fromisoformat(config.LIVE_REPLAY_START)
            self._replay_epoch0 = int((start - datetime(1970, 1, 1)).total_seconds()) - config.IST_OFFSET_S
            self._wall0 = time.time()
        elif mode == "gtfsrt":
            threading.Thread(target=self._poll_loop, daemon=True).start()

    # ------------------------------------------------------------------ clock
    def now_epoch(self):
        if self.mode == "replay":
            return self._replay_epoch0 + (time.time() - self._wall0)
        return time.time()

    def now_ist(self):
        return datetime(1970, 1, 1) + timedelta(seconds=self.now_epoch() + config.IST_OFFSET_S)

    # ------------------------------------------------------------------ sources
    def _poll_loop(self):
        try:
            import requests
            from google.transit import gtfs_realtime_pb2
        except ImportError:
            print("[live] pip install requests gtfs-realtime-bindings  to enable GTFS-RT")
            return
        headers = {"Authorization": config.GTFSRT_API_KEY} if config.GTFSRT_API_KEY else {}
        while True:
            try:
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(requests.get(config.GTFSRT_URL, headers=headers, timeout=20).content)
                rows = []
                for e in feed.entity:
                    if e.HasField("vehicle"):
                        v = e.vehicle
                        rows.append((v.vehicle.id, v.position.latitude, v.position.longitude,
                                     v.timestamp or int(time.time()), v.trip.route_id, v.trip.trip_id))
                cut = time.time() - config.LIVE_WINDOW_S - 300
                with self._lock:
                    self._buffer = [r for r in self._buffer if r[3] >= cut] + rows
            except Exception as ex:                      # keep polling on network errors
                print("[live] poll error:", ex)
            time.sleep(20)

    def _positions(self, now):
        if self.mode == "replay":
            return gps.read_window(config.LIVE_REPLAY_DB, now - config.LIVE_WINDOW_S, now)
        with self._lock:
            rows = list(self._buffer)
        return gps.normalise(pd.DataFrame(rows, columns=["vehicle", "lat", "lon", "ts", "route", "trip"]))

    # ------------------------------------------------------------------ nowcast
    def refresh(self, force=False):
        if self.mode == "off" or (not force and time.time() - self._last < 60):
            return
        self._last = time.time()
        now = self.now_epoch()
        pts = self._positions(now)
        if pts.empty:
            return
        pts = gps.clean(pts)
        day = self.now_ist().date()
        dstart = gps.ist_midnight_epoch(day)
        trav = match_points(pts, self.net, dstart, trip_to_pattern=self._t2p)
        self.n_vehicles = int(pts["vehicle"].nunique())
        self.n_traversals = int(len(trav))
        if trav.empty:
            return
        d = gps.daytype(day)
        seg = trav["seg"].to_numpy()
        lr = np.log(trav["dt"].to_numpy(float) / self.model.mu[seg, d, tbin(trav["t"].to_numpy())])
        g = pd.DataFrame({"seg": seg, "lr": lr}).groupby("seg")["lr"].agg(["median", "size"])
        self.city_log_ratio = float(np.median(lr))
        self.factors = dict(zip(g.index.to_numpy().tolist(), g["median"].to_numpy().tolist()))
        self.counts = dict(zip(g.index.to_numpy().tolist(), g["size"].to_numpy().tolist()))

    def multiplier_fn(self, now_service_t):
        """Return f(seg, t_service) -> multiplicative factor on the historical time."""
        glob = self.city_log_ratio

        def f(seg, t):
            w = np.exp(-max(0.0, t - now_service_t) / config.LIVE_DECAY_S)
            if seg in self.factors:
                n = self.counts[seg]
                lr = (n * self.factors[seg] + 2 * glob) / (n + 2)
            else:
                lr = 0.5 * glob
            return float(np.exp(w * lr))
        return f

    def status(self):
        return {
            "mode": self.mode,
            "now_ist": self.now_ist().strftime("%Y-%m-%d %H:%M") if self.mode != "off" else None,
            "vehicles_seen": self.n_vehicles,
            "segments_with_live_data": len(self.factors),
            "city_speed_vs_usual_pct": round(float(100 * (np.exp(-self.city_log_ratio) - 1)), 1),
        }
