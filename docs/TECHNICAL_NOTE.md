# Technical note

**Problem 1: Real-time bus traffic model and journey planner (Delhi)**

The approach treats every DTC bus as a moving traffic sensor. The system learns how long each stretch of road between two bus stops takes, for each time of day and day type. It then plans journeys on a timetable whose running times are re-predicted from those learned times.

```
GPS points ──► clean ──► map-match to route pattern ──► stop-crossing times ──► segment traversals
                                                                                     │
GTFS (stops, trips, shapes) ──► patterns + segments ──────────────────────────────►  model
                                                                                     │
           live GTFS-RT ──► same matcher ──► live / historical ratio ──► nowcast ────┤
                                                                                     ▼
             query ──► access walks ──► RAPTOR on GPS-calibrated timetable ──► options + uncertainty
```

## 1. Linking GPS points to roads and routes

1. **Cleaning** (`transit/gps.py`)
   - Points outside the NCR bounding box are dropped.
   - Duplicates are removed.
   - Data is down-sampled to one point per 10 s.
   - Spikes are removed: a point is a spike when reaching it, and leaving it, would need a speed above 90 km/h.
2. **Patterns** (`transit/gtfs.py`)
   - Each unique stop sequence of a route is one *pattern* (direction or variant).
   - Its geometry is the GTFS `shapes.txt` line. If the shape doesn't fit the stops, the line joining the stops is used instead.
   - Each stop gets a distance along the geometry. These distances are computed in sequence, so they never go backwards; this handles loops at terminals.
3. **Matching** (`transit/matcher.py`)
   - A vehicle's consecutive points on the same `route_id` form a block.
   - Each block is projected onto every pattern of that route.
   - A point is kept only if it lies within 70 m of the road shape.
   - A **run** is a maximal sequence of kept points that keeps moving *forward* along the pattern. "Forward" is checked with a ±3-point smoothed distance change, so GPS jitter at stops is tolerated.
   - A run breaks on a gap of more than 5 minutes or a backward jump. This separates the two directions and successive trips.
   - When runs from two patterns overlap in time, the one that crosses more stops wins.
4. **Stop-crossing times**
   - The distance along the route is made non-decreasing with a running maximum.
   - The time the bus passes each stop is linearly interpolated between the two GPS points either side of it.
   - A crossing is only accepted if those two points are less than 3 minutes apart.
5. **Traversals**
   - Consecutive crossings give stop-to-stop travel times.
   - Implausible speeds (below 1.4 km/h or above 80 km/h) are rejected.

**Waiting at stops and signals.** We deliberately **do not separate** dwell and signal time. A stop-to-stop traversal runs from one stop *passage* to the next, so it already contains the dwell at the downstream stop and every signal on the way. That is exactly what a passenger on board experiences, and it needs no stop-detection heuristics.

**Waiting for the bus.** This is modelled separately, from observed headways. Using the waiting-time paradox, E[wait] = E[H²] / 2E[H]. This captures bus bunching, which makes real waits longer than half the scheduled headway.

## 2. Estimating travel time on each stretch of road

**Segments.** A segment is an ordered stop pair (A → B). It is **pooled across all routes** that use it, so a busy corridor gets data from dozens of routes.

**Cells.** For every segment *s*, day type *d* (weekday / Saturday / Sunday or holiday) and 30-minute bin *b*, we work in log space:

```
log t[s,d,b] = prior[s,d,b] + n/(n+k) · (observed median − prior)      (empirical-Bayes shrinkage, k = 4)
prior[s,d,b] = base[s] + F[d,b]
```

- **F[d,b]** is the **city congestion profile**: the median, over the whole fleet, of each observation's deviation from its segment's all-day level.
- **base[s]** is the segment's all-day median.
  - If the segment was never observed, base[s] comes from its length (using the fleet's median speed), averaged with its scheduled time after calibrating the schedule against observed segments.
- **Smoothing.** The shrunken residuals are smoothed across neighbouring time bins, which removes noise at bin boundaries.
- **Clipping.** Estimates are clipped to physically plausible speeds.
- **Mean, not median.** Delays are right-skewed, so adding up segment medians would under-estimate a whole trip. Each segment therefore uses the log-normal mean, exp(μ + σ²/2). This reduces the under-estimation of whole-trip times.
- **Uncertainty.** For each cell we keep the log-sd, shrunk towards the city-wide spread for that bin. A trip's spread combines its segments with an intra-trip correlation of ρ = 0.6:
  σ² = (1 − ρ)Σσᵢ² + ρ(Σσᵢ)².
  The API reports the resulting 10th–90th percentile range.

**Sparse data.** The fallback is automatic. The n/(n+k) weight moves smoothly from data, to the segment's own all-day level shifted by the city's congestion at that hour, to the length- and schedule-based estimate for never-observed segments. There is no hard switch and no wild estimate.

**Time propagation.** A journey is predicted segment by segment. Each segment uses the bin in which the bus is *predicted to enter it*, so a 08:00 departure meets the 08:30–09:00 congestion on the stretches it reaches at 08:40.

## 3. Combining live and historical data

**Historical prediction.** This is the model above.

**Live nowcast** (`transit/live.py`)
- Live positions from the last 20 minutes go through the **same matcher**. They come from the GTFS-RT feed, or from a historical day in replay mode.
- For every segment a bus has just completed, we compute r = log(observed / historical).
- Each segment's median r is shrunk towards the city-wide median r. Segments without live data get half of the city-wide value.
- Predicted times are multiplied by exp(w · r), where w = exp(−Δt / 30 min) and Δt is how far in the future the segment will be traversed. Live conditions therefore dominate the next few minutes and fade back to the historical pattern.
- A `replay` mode feeds a historical day through the same code as if it were live. We use it for the demo and for an honest backtest: the live signal only ever uses buses that finished a segment **before** the journey started.

## 4. Journey planning

**GPS-calibrated timetable.**
- Each scheduled trip keeps its GTFS departure from the first stop.
- Every later stop time is re-predicted by time propagation, separately for each day type.
- First-in-first-out order is enforced, so buses on the same pattern cannot overtake each other.

**RAPTOR** (Delling, Pajor & Werneck, 2012)
- Finds Pareto-optimal journeys: the earliest arrival for 0, 1 and 2 transfers.
- Allows walking transfers of up to 250 m and a 60 s minimum time to change buses.
- To get a variety of options, the search is re-run with the patterns used by the fastest journey banned.

**Access and egress.** All stops within 800 m of the origin and destination are considered, at a walking speed of 4.5 km/h with a 1.25 detour factor. The radius widens automatically if no stop is found.

**What each option reports:**
- walk, wait and ride times
- an 80% range
- the timetable's estimate
- the time at the quietest hour of that day, which shows the effect of traffic
- the bus frequency actually observed on that route
- the share of the ride learned from GPS
- the route geometry

**Profile.** The API also gives door-to-door time for every departure through the day ("best time to leave").

## 5. Validation (see [VALIDATION.md](VALIDATION.md))

**Backtest on unseen days.**
- Train on some days and test on later held-out days.
- Results: the mean absolute error is 5.65 min for the timetable, 4.73 min for the static average, 2.84 min for the model and 2.61 min for the model with live nowcast.
- Test journeys are real: random stretches of 5–30 stops on buses observed on the test days.
- Four methods are compared: the GTFS timetable, a time-of-day-agnostic average, our model, and our model with live nowcast.
- Reported metrics: MAE, MAPE, share within ±10%, share within ±5 min, and bias, broken down by time of day.

**Google Maps comparison.** Origin–destination pairs and Google's times go in `validation_pairs.csv`; `scripts/compare_google.py` produces the comparison table.

## 6. Extensions

- Metro legs (multimodal RAPTOR).
- Crowding estimates (Problem 2).
- Weather- and event-aware corrections.
- Per-vehicle arrival prediction for the next bus.
