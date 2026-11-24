@echo off
REM ---- Edit these two paths, then double-click or run:  run_pipeline.bat
set GPS_DIR=C:\Users\%USERNAME%\Downloads\delhi-dtc-bus-gtfs-rt-2026-10
set GTFS_DIR=C:\Users\%USERNAME%\Downloads\GTFS

python scripts\prepare_network.py || exit /b 1
python scripts\match_gps.py --days all --workers 4 || exit /b 1
python scripts\backtest.py --train 2026-10-01:2026-10-24 --test 2026-10-25:2026-10-31 || exit /b 1
python scripts\build_model.py --train all || exit /b 1
echo.
echo Done. Start the app with:  python -m uvicorn api.app:app --port 8000   then open http://localhost:8000
