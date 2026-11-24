import sys, pandas as pd, numpy as np
d = pd.read_pickle(sys.argv[1])
print("rows", len(d), "runs", d["run"].nunique(), "segments", d["seg"].nunique(), "patterns", d["pattern"].nunique())
print("dt quantiles (s):", np.percentile(d["dt"], [5, 25, 50, 75, 95]).round(0))
print("runs per pattern median:", d.groupby("pattern")["run"].nunique().median())
print("segments per run median:", d.groupby("run").size().median())
h = (d["t"] // 3600).astype(int)
print("by hour:", h.value_counts().sort_index().to_dict())
