"""Step 4 - Honest accuracy test on unseen days.

Trains the model on the --train days, then replays real bus trips from the --test days.
For thousands of randomly chosen journeys (5-30 stops) on real buses it compares the
observed time with:
    timetable   : the GTFS schedule (what Delhi's published timetable says)
    static      : average segment times, ignoring time of day (a typical "historical average" app)
    model       : our time-of-day / day-type model with time propagation
    model+live  : model + live nowcast using only buses seen in the 20 min BEFORE the journey

    python scripts/backtest.py --train 2026-10-01:2026-10-24 --test 2026-10-25:2026-10-31

Writes docs/VALIDATION.md, docs/img/*.png and artifacts/backtest.json.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
from scripts.build_model import load_traversals  # noqa: E402
from transit.gtfs import Network  # noqa: E402
from transit.model import TravelTimeModel, tbin  # noqa: E402

BANDS = [("Early 05-08", 5, 8), ("AM peak 08-11", 8, 11), ("Midday 11-17", 11, 17),
         ("PM peak 17-21", 17, 21), ("Night 21-24", 21, 24)]
METHODS = ["timetable", "static", "model", "model_live"]
LABEL = {"timetable": "GTFS timetable", "static": "Static average (no time of day)",
         "model": "Our model (historical)", "model_live": "Our model + live nowcast"}


def nowcast_tables(day_df, model, step=300):
    """For each 5-min step: {seg: (median log-ratio, n)} and city median, from traversals that
    FINISHED in the previous LIVE_WINDOW_S seconds (information available in real time)."""
    d = int(day_df["dtype"].iloc[0])
    seg = day_df["seg"].to_numpy()
    lr = np.log(day_df["dt"].to_numpy(float) / model.mu[seg, d, tbin(day_df["t"].to_numpy())])
    exit_t = day_df["t"].to_numpy() + day_df["dt"].to_numpy()
    o = np.argsort(exit_t)
    exit_t, seg, lr = exit_t[o], seg[o], lr[o]
    tables = {}
    for T in range(4 * 3600, 25 * 3600, step):
        a, b = np.searchsorted(exit_t, [T - config.LIVE_WINDOW_S, T])
        if b - a < 5:
            continue
        g = pd.DataFrame({"s": seg[a:b], "lr": lr[a:b]}).groupby("s")["lr"].agg(["median", "size"])
        tables[T] = (dict(zip(g.index, zip(g["median"], g["size"]))), float(np.median(lr[a:b])))
    return tables


def metrics(err, actual):
    ae = np.abs(err)
    return {"n": int(len(err)), "MAE_min": round(float(ae.mean() / 60), 2),
            "MAPE_pct": round(float(np.mean(ae / actual) * 100), 1),
            "within_10pct": round(float(np.mean(ae / actual <= 0.10) * 100), 1),
            "within_5min": round(float(np.mean(ae <= 300) * 100), 1),
            "bias_min": round(float(err.mean() / 60), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--test", required=True)
    ap.add_argument("--samples", type=int, default=30000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)

    net = Network.load(config.ARTIFACTS / "network.pkl")
    train, tdays = load_traversals(a.train)
    test, sdays = load_traversals(a.test)
    print(f"train days: {len(tdays)}   test days: {len(sdays)}")
    model = TravelTimeModel.build(train, net)

    # scheduled reference per pattern
    rows = []
    per_run_cap = max(1, int(np.ceil(a.samples / max(1, test.groupby(["date", "run"]).ngroups) * 1.5)))
    for day, ddf in test.groupby("date"):
        tables = nowcast_tables(ddf, model)
        steps = np.array(sorted(tables))
        d = int(ddf["dtype"].iloc[0])
        for run, r in ddf.sort_values(["run", "pos"]).groupby("run"):
            pos = r["pos"].to_numpy()
            brk = np.r_[True, np.diff(pos) != 1]
            starts = np.flatnonzero(brk)
            ends = np.r_[starts[1:], len(pos)]
            for s0, e0 in zip(starts, ends):
                if e0 - s0 < 5:
                    continue
                for _ in range(per_run_cap):
                    L = int(rng.integers(5, min(30, e0 - s0) + 1))
                    i = int(rng.integers(s0, e0 - L + 1))
                    seg = r["seg"].to_numpy()[i:i + L]
                    t0 = float(r["t"].iloc[i])
                    actual = float(r["dt"].to_numpy()[i:i + L].sum())
                    p = net.patterns[int(r["pattern"].iloc[0])]
                    pi = int(pos[i])
                    S = p["sched"]
                    j = int(np.argmin(np.abs(S[:, pi] - t0)))
                    sched = float(S[j, pi + L] - S[j, pi])
                    static = float(model.base_static[seg].sum())
                    pm = model.propagate(seg, t0, d)[-1] - t0
                    # live: tables from the last step strictly before t0
                    k = np.searchsorted(steps, t0) - 1
                    if k >= 0:
                        tab, glob = tables[steps[k]]
                        T = steps[k]

                        def mult(sg, tt, tab=tab, glob=glob, T=T):
                            w = np.exp(-max(0.0, tt - T) / config.LIVE_DECAY_S)
                            if sg in tab:
                                m, n = tab[sg]
                                lr = (n * m + 2 * glob) / (n + 2)
                            else:
                                lr = 0.5 * glob
                            return float(np.exp(w * lr))
                        pl = model.propagate(seg, t0, d, mult)[-1] - t0
                    else:
                        pl = pm
                    rows.append((day, d, t0, L, actual, sched, static, pm, pl))
    df = pd.DataFrame(rows, columns=["date", "dtype", "t0", "n_stops", "actual", "timetable", "static",
                                     "model", "model_live"])
    df = df[np.isfinite(df["timetable"]) & (df["timetable"] > 0)]
    if len(df) > a.samples:
        df = df.sample(a.samples, random_state=a.seed)
    print(f"evaluated journeys: {len(df):,}")

    res = {"overall": {}, "by_band": {}, "train_days": [str(x) for x in tdays],
           "test_days": [str(x) for x in sdays], "model_coverage_pct": model.meta["coverage_pct"]}
    for m in METHODS:
        res["overall"][m] = metrics(df[m].to_numpy() - df["actual"].to_numpy(), df["actual"].to_numpy())
    hr = (df["t0"] // 3600) % 24
    for name, h0, h1 in BANDS:
        sub = df[(hr >= h0) & (hr < h1)]
        if len(sub) >= 20:
            res["by_band"][name] = {m: metrics(sub[m].to_numpy() - sub["actual"].to_numpy(),
                                               sub["actual"].to_numpy()) for m in METHODS}
    (config.ARTIFACTS / "backtest.json").write_text(json.dumps(res, indent=2))
    df.to_csv(config.ARTIFACTS / "backtest_samples.csv", index=False)
    write_report(res, df, model)
    o = res["overall"]
    print("\nMAE (minutes):", {m: o[m]["MAE_min"] for m in METHODS})
    print("MAPE (%):     ", {m: o[m]["MAPE_pct"] for m in METHODS})
    print(f"\nReport: {config.DOCS / 'VALIDATION.md'}")


def write_report(res, df, model):
    img = config.DOCS / "img"
    img.mkdir(parents=True, exist_ok=True)
    plots = []
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        # 1. MAE by band
        bands = list(res["by_band"])
        fig, ax = plt.subplots(figsize=(9, 4.2))
        w = 0.2
        for k, m in enumerate(METHODS):
            ax.bar(np.arange(len(bands)) + (k - 1.5) * w, [res["by_band"][b][m]["MAE_min"] for b in bands],
                   w, label=LABEL[m])
        ax.set_xticks(np.arange(len(bands)))
        ax.set_xticklabels(bands)
        ax.set_ylabel("Mean absolute error (min)")
        ax.set_title("Journey-time error by time of day (unseen test days)")
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(img / "mae_by_band.png", dpi=130)
        plt.close(fig)
        plots.append(("Error by time of day", "img/mae_by_band.png"))
        # 2. predicted vs actual
        s = df.sample(min(3000, len(df)), random_state=1)
        fig, axs = plt.subplots(1, 2, figsize=(9, 4.2), sharey=True)
        for ax, m in zip(axs, ("timetable", "model_live")):
            ax.scatter(s["actual"] / 60, s[m] / 60, s=3, alpha=0.3)
            mx = float(np.percentile(s["actual"] / 60, 99))
            ax.plot([0, mx], [0, mx], "k--", lw=1)
            ax.set_xlim(0, mx)
            ax.set_ylim(0, mx)
            ax.set_title(LABEL[m])
            ax.set_xlabel("Actual (min)")
        axs[0].set_ylabel("Predicted (min)")
        fig.tight_layout()
        fig.savefig(img / "pred_vs_actual.png", dpi=130)
        plt.close(fig)
        plots.append(("Predicted vs actual", "img/pred_vs_actual.png"))
        # 3. city congestion profile
        fig, ax = plt.subplots(figsize=(9, 3.6))
        hours = np.arange(config.N_BINS) * config.BIN_S / 3600
        for d, nm in enumerate(["Weekday", "Saturday", "Sunday/holiday"]):
            ax.plot(hours, np.exp(model.F[d] - model.F[d].min()), label=nm)
        ax.set_xlabel("Hour of day")
        ax.set_ylabel("Travel-time index (1 = fastest)")
        ax.set_title("Delhi bus congestion profile learned from the fleet")
        ax.set_xlim(4, 24)
        ax.legend()
        fig.tight_layout()
        fig.savefig(img / "congestion_profile.png", dpi=130)
        plt.close(fig)
        plots.append(("City congestion profile", "img/congestion_profile.png"))
    except ImportError:
        pass

    o = res["overall"]
    lines = ["# Validation report", "",
             f"*Train days:* {res['train_days'][0]} → {res['train_days'][-1]} ({len(res['train_days'])} days)  ",
             f"*Test days (unseen):* {res['test_days'][0]} → {res['test_days'][-1]} ({len(res['test_days'])} days)  ",
             f"*Journeys evaluated:* {o['model']['n']:,} real bus journeys of 5–30 stops, sampled from GPS-observed trips  ",
             f"*Road segments with GPS data:* {res['model_coverage_pct']}%", "",
             "## Overall", "",
             "| Method | MAE (min) | MAPE | within ±10% | within ±5 min | bias (min) |",
             "|---|---|---|---|---|---|"]
    for m in METHODS:
        r = o[m]
        lines.append(f"| {LABEL[m]} | {r['MAE_min']} | {r['MAPE_pct']}% | {r['within_10pct']}% | "
                     f"{r['within_5min']}% | {r['bias_min']:+} |")
    imp = 100 * (1 - o["model_live"]["MAE_min"] / max(o["timetable"]["MAE_min"], 1e-9))
    lines += ["", f"**Error reduced by {imp:.0f}% versus the published timetable.**", "",
              "## By time of day (MAE, minutes)", "",
              "| Band | " + " | ".join(LABEL[m] for m in METHODS) + " |",
              "|---|" + "---|" * len(METHODS)]
    for b, r in res["by_band"].items():
        lines.append(f"| {b} | " + " | ".join(str(r[m]["MAE_min"]) for m in METHODS) + " |")
    lines += ["", "## Method", "",
              "* Test journeys are real: a random stretch of 5–30 consecutive stops on a bus that was "
              "actually observed on a test day; *actual* = observed GPS time.",
              "* The model never sees test days. The live variant only uses buses that finished a segment "
              "in the 20 minutes **before** the journey starts (no look-ahead).",
              "* Timetable baseline = the scheduled trip of the same pattern closest in time.", ""]
    for title, pth in plots:
        lines += [f"### {title}", "", f"![{title}]({pth})", ""]
    lines += ["## Google Maps comparison", "",
              "See [GOOGLE_COMPARISON.md](GOOGLE_COMPARISON.md) (generated by `scripts/compare_google.py`)."]
    config.DOCS.mkdir(exist_ok=True)
    (config.DOCS / "VALIDATION.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
