import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from transit.gtfs import Network
net = Network.load(config.ARTIFACTS / "network.pkl")
for q in sys.argv[1:]:
    print(q, "->", [(h["name"], h["n_stop_ids"]) for h in net.search_stops(q, limit=6)])
