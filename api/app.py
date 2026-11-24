"""
REST API for the Delhi bus journey planner.

    uvicorn api.app:app --port 8000
    open http://localhost:8000        (web app)
    open http://localhost:8000/docs   (interactive API documentation)
"""
import json
import sys
from datetime import datetime
from typing import Optional
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

import config  # noqa: E402
from transit.planner import Planner  # noqa: E402

app = FastAPI(
    title="Safar · Delhi Bus Journey API",
    version="1.0",
    description=(
        "Bus journey options for Delhi with travel times learned from real DTC bus GPS traces "
        "(time-of-day and day-type aware, time-propagated, optional live nowcast)."
    ),
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
planner = Planner()
WEB = config.ROOT / "web"


def parse_depart(depart: Optional[str]) -> datetime:
    if not depart:
        return planner.live.now_ist() if planner.live else datetime.now()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%H:%M"):
        try:
            d = datetime.strptime(depart, fmt)
            if fmt == "%H:%M":
                today = datetime.now()
                d = d.replace(year=today.year, month=today.month, day=today.day)
            return d
        except ValueError:
            continue
    raise HTTPException(400, "depart must look like 2026-10-27T08:30 or 08:30")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html")


@app.get("/journeys", summary="Plan bus journeys between two points")
def journeys(
    from_: str = Query(..., alias="from", description="'lat,lng' or a stop name, e.g. 'Kashmere Gate ISBT'"),
    to: str = Query(..., description="'lat,lng' or a stop name, e.g. 'Nehru Place'"),
    depart: Optional[str] = Query(None, description="Departure time (IST) e.g. 2026-10-27T08:30; default now"),
    max_transfers: int = Query(config.MAX_TRANSFERS, ge=0, le=3),
    options: int = Query(3, ge=1, le=5),
):
    """Returns up to `options` journeys. Each has walk / wait / ride / transfer breakdown,
    total time with an 80% range, the timetable's estimate, the usual time at the quietest hour,
    and per-leg details (route, stops, departure / arrival, path geometry, observed bus frequency)."""
    try:
        return planner.plan(from_, to, parse_depart(depart), max_transfers=max_transfers, n_options=options)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/profile", summary="Journey time across the day (best time to leave)")
def profile(from_: str = Query(..., alias="from"), to: str = Query(...), depart: Optional[str] = None,
            step_min: int = Query(15, ge=5, le=60)):
    try:
        return planner.profile(from_, to, parse_depart(depart), step_min=step_min)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/stops/search", summary="Find stops by name")
def stops_search(q: str, limit: int = Query(8, ge=1, le=30)):
    return planner.net.search_stops(q, limit)


@app.get("/congestion", summary="Bus speeds on every observed road segment at a time of day")
def congestion(day_type: str = Query("weekday", description="weekday | saturday | sunday"),
               time: str = Query("09:00", description="HH:MM")):
    try:
        h, m = (int(x) for x in time.split(":"))
        d = {"weekday": 0, "saturday": 1, "sunday": 2}[day_type]
    except (ValueError, KeyError):
        raise HTTPException(400, "day_type must be weekday|saturday|sunday and time HH:MM")
    return planner.congestion(d, h * 3600 + m * 60)


@app.get("/city-profile", summary="City-wide congestion index by time of day")
def city_profile():
    return planner.city_profile()


@app.get("/live/status", summary="Live nowcast status")
def live_status():
    if planner.live is None:
        return {"mode": "off"}
    planner.live.refresh()
    return planner.live.status()


@app.get("/meta", summary="Dataset, model and validation summary")
def meta():
    out = {"model": planner.model.meta, "stops": len(planner.net.stops), "routes": len(planner.net.routes),
           "patterns": len(planner.net.patterns), "segments": len(planner.net.seg_from)}
    bt = config.ARTIFACTS / "backtest.json"
    if bt.exists():
        b = json.loads(bt.read_text())
        out["validation"] = {"overall": b["overall"], "test_days": b["test_days"]}
    return out


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}
