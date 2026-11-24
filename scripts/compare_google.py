"""Step 5 - Compare our journey times with Google Maps (and observed trips, if you have them).

Fill validation_pairs.csv:  name, from, to, depart, google_min, observed_min(optional)
  * from / to : "lat,lng" or a stop name
  * depart    : "2026-10-28 08:30"  (use a test day; read Google's transit time for that weekday+time)
  * google_min: total door-to-door minutes Google Maps shows (Transit -> Bus only, "Depart at")

    python scripts/compare_google.py
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
from transit.planner import Planner  # noqa: E402


def main():
    src = config.ROOT / "validation_pairs.csv"
    pairs = pd.read_csv(src)
    planner = Planner(live_mode="off")
    rows = []
    for _, r in pairs.iterrows():
        dep = datetime.strptime(str(r["depart"]).strip(), "%Y-%m-%d %H:%M")
        try:
            res = planner.plan(str(r["from"]), str(r["to"]), dep)
        except ValueError as e:
            print("skip", r["name"], e)
            continue
        if not res["options"]:
            print("no journey for", r["name"])
            continue
        o = res["options"][0]
        rows.append({
            "pair": r["name"], "depart": r["depart"], "route": o["summary"],
            "ours_min": o["total_min"], "ours_range": f"{o['range_min'][0]}–{o['range_min'][1]}",
            "timetable_min": o["timetable_estimate_min"],
            "google_min": r.get("google_min", np.nan),
            "observed_min": r.get("observed_min", np.nan),
        })
    df = pd.DataFrame(rows)
    df.to_csv(config.ARTIFACTS / "google_comparison.csv", index=False)
    lines = ["# Comparison with Google Maps", "",
             "| Pair | Depart | Our route | Ours (min) | 80% range | Timetable | Google Maps | Observed |",
             "|---|---|---|---|---|---|---|---|"]
    for r in df.itertuples():
        f = lambda v: "–" if pd.isna(v) else f"{v:.0f}"
        lines.append(f"| {r.pair} | {r.depart} | {r.route} | {r.ours_min:.0f} | {r.ours_range} | "
                     f"{f(r.timetable_min)} | {f(r.google_min)} | {f(r.observed_min)} |")
    has_obs = df["observed_min"].notna() if "observed_min" in df else pd.Series(False, index=df.index)
    if has_obs.any():
        sub = df[has_obs]
        lines += ["", "**Mean absolute error vs observed trips (min):** "
                  f"ours {np.mean(np.abs(sub.ours_min - sub.observed_min)):.1f} · "
                  f"Google {np.nanmean(np.abs(sub.google_min - sub.observed_min)):.1f} · "
                  f"timetable {np.mean(np.abs(sub.timetable_min - sub.observed_min)):.1f}"]
    if df["google_min"].notna().any():
        g = df[df["google_min"].notna()]
        lines += ["", f"**Mean difference ours − Google:** {np.mean(g.ours_min - g.google_min):+.1f} min "
                  f"(mean absolute {np.mean(np.abs(g.ours_min - g.google_min)):.1f} min)"]
    (config.DOCS / "GOOGLE_COMPARISON.md").write_text("\n".join(lines), encoding="utf-8")
    print(df.to_string(index=False))
    print(f"\nWritten {config.DOCS / 'GOOGLE_COMPARISON.md'}")


if __name__ == "__main__":
    main()
