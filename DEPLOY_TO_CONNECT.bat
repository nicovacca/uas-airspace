@echo off
rem Double-click to publish the map to Posit Connect as a Streamlit app behind a shared password.
rem Run RUN_MAP.bat first (it downloads the data and builds the map).
cd /d "%~dp0"
title Publish Drone Airspace Map to Posit Connect
echo.
echo  ===== Publish to Posit Connect =====
echo.

if not exist "v2_full\data\states.js" goto :need_map
python --version >nul 2>&1
if errorlevel 1 (
  echo  Python is not installed. Get it from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH" in the installer. Then run this file again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" python -m venv .venv || goto :fail

echo  [1/3] Getting the publishing tool ready...
".venv\Scripts\python" -m pip install --quiet --disable-pip-version-check rsconnect-python || goto :fail
".venv\Scripts\python" deploy\build_site.py streamlit_app || goto :fail

echo.
echo  [2/3] Three questions. Nothing you type here is saved in any file.
echo.
set CONNECT_SERVER=
set CONNECT_API_KEY=
set MAP_PASSWORD=
set /p CONNECT_SERVER=  Posit Connect address, e.g. https://connect.yourcompany.com :
set /p CONNECT_API_KEY=  Your Connect API key (Connect: your name, top right, then API Keys) :
set /p MAP_PASSWORD=  Password people will type to open the map :
if not defined CONNECT_SERVER goto :missing
if not defined CONNECT_API_KEY goto :missing
if not defined MAP_PASSWORD goto :missing

echo.
echo  [3/3] Uploading the map (about 100 MB, a few minutes)...
".venv\Scripts\rsconnect" deploy streamlit streamlit_app --entrypoint app.py --title "Drone Airspace Map" -E MAP_PASSWORD || goto :fail

echo.
echo  Published. Last step, in Posit Connect:
echo    open the "Drone Airspace Map" content, go to Settings, then Access,
echo    and choose "Anyone - no login required". The password page protects it.
echo.
echo  To update the hosted map later: refresh the data with RUN_MAP.bat, then run this file again.
echo.
pause
exit /b 0

:need_map
echo  The map data folder v2_full\data is missing.
echo  Double-click RUN_MAP.bat first to download and build it, then run this file again.
echo.
pause
exit /b 1

:missing
echo.
echo  All three answers are needed. Run this file again.
echo.
pause
exit /b 1

:fail
echo.
echo  Something went wrong. Take a screenshot of this window and send it to whoever gave you the map.
echo.
pause
exit /b 1
