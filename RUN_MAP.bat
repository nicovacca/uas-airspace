@echo off
rem Double-click this file to set up, download the FAA data, build the map and open it.
cd /d "%~dp0"
title Drone Airspace Map
echo.
echo  ===== Drone Airspace Map =====
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo  Python is not installed yet.
  echo.
  echo  1. Go to https://www.python.org/downloads/ and click the big yellow Download button.
  echo  2. Run the installer and TICK the box "Add python.exe to PATH" at the bottom.
  echo  3. Then double-click RUN_MAP.bat again.
  echo.
  if not "%NO_OPEN%"=="1" start "" https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo  [1/4] First-time setup: installing what the map needs. This takes a few minutes...
  python -m venv .venv || goto :fail
  ".venv\Scripts\python" -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :fail
) else (
  echo  [1/4] Setup already done.
)

set AREA=
if /i "%~1"=="--test" set AREA=--bbox=-74.1,40.6,-73.8,40.9

echo  [2/4] Downloading FAA drone data for the whole U.S. This takes about 30 minutes.
echo        Leave this window open. "rate-limited, waiting 60s" messages are normal.
".venv\Scripts\python" build_uas_layers.py --out out_full %AREA%
if errorlevel 1 echo  Note: a few layers could not be downloaded. The map will still be built with the rest.

echo  [3/4] Building the map...
".venv\Scripts\python" make_v2_full.py || goto :fail

echo  [4/4] Opening the map in your browser...
if not "%NO_OPEN%"=="1" start "" "%~dp0v2_full\index.html"
echo.
echo  Done. Next time you can just open v2_full\index.html,
echo  or double-click RUN_MAP.bat again to get fresh data.
echo.
pause
exit /b 0

:fail
echo.
echo  Something went wrong. Take a screenshot of this window and send it to whoever gave you the map.
echo.
pause
exit /b 1
