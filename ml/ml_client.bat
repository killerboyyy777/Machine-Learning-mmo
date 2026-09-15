@echo off
setlocal
title Text MMO - ML Client
cd /d "%~dp0"
set "PYTHONUNBUFFERED=1"

rem Start the engine first:  ..\start.bat  (or: python ..\server.py)

where python >nul 2>nul
if %errorlevel%==0 ( set "PY=python" ) else ( set "PY=py" )

echo ============================================================
echo  ML Client - online Q-learning agent (trains a character)
echo ============================================================
echo.
echo  Make sure the engine is running (..\start.bat).
echo  Pass any ml_client.py args after this script, e.g.:
echo    ml_client.bat --name MLAgent --steps 5000
echo ============================================================
echo.

%PY% ml_client.py %*
set "EXIT=%errorlevel%"
echo.
echo ML client exited with code %EXIT%.
pause