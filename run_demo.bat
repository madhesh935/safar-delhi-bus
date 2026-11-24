@echo off
REM Smoke test on synthetic data (no downloads needed)
set GPS_DIR=demo_data\gps
set GTFS_DIR=demo_data\gtfs
python scripts\make_synthetic.py || exit /b 1
python scripts\prepare_network.py || exit /b 1
python scripts\match_gps.py --workers 1 || exit /b 1
python scripts\backtest.py --train 2026-10-01:2026-10-07 --test 2026-10-08:2026-10-10 || exit /b 1
python scripts\build_model.py --train all || exit /b 1
python -m uvicorn api.app:app --port 8000
