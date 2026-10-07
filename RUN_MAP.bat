@echo off
rem Double-click this file to open the map. The first time it also sets up and downloads the FAA data.
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

if exist "out_full\uas_rules.gpkg" goto :have_data
echo  [2/4] Downloading FAA drone data for the whole U.S. This takes about 30 minutes, first time only.
echo        Leave this window open. "rate-limited, waiting 60s" messages are normal.
".venv\Scripts\python" build_uas_layers.py --out out_full %AREA%
if errorlevel 1 echo  Note: a few layers could not be downloaded. The map will still be built with the rest.
goto :build

:have_data
echo  [2/4] FAA data already downloaded. Use the Refresh buttons at the top of the map to update it.

:build
if exist "v2_full\data\states.js" if exist "out_full\uas_rules.gpkg" if not "%~1"=="--test" goto :open
echo  [3/4] Building the map...
".venv\Scripts\python" make_v2_full.py || goto :fail

:open
echo  [4/4] Opening the map in your browser.
echo.
echo  KEEP THIS WINDOW OPEN while you use the map - it powers the Refresh buttons.
echo  Close this window when you are done.
echo.
if "%NO_SERVE%"=="1" exit /b 0
set BROWSER_FLAG=
if "%NO_OPEN%"=="1" set BROWSER_FLAG=--no-browser
".venv\Scripts\python" serve_map.py %BROWSER_FLAG% || goto :fail
exit /b 0

:fail
echo.
echo  Something went wrong. Take a screenshot of this window and send it to whoever gave you the map.
echo.
pause
exit /b 1
