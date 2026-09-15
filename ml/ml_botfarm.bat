@echo off
setlocal
title Text MMO - ML Bot Farm
cd /d "%~dp0"

rem Make sure the engine is running (once).  The farm will keep restarting
rem only when it exits with errorlevel 0 (steps exhausted / normal end).
rem Press Ctrl+C in the farm window to stop it completely.

where python >nul 2>nul
if %errorlevel%==0 ( set "PY=python" ) else ( set "PY=py" )

echo ============================================================
echo  ML Bot Farm - train bots, restart only on normal exit
echo ============================================================
echo.
echo  Make sure the engine is running (..\start.bat).
echo  Default: 4 bots until stopped.  Override with --bots, --steps, etc.
echo    ml_botfarm.bat --bots 8 --steps 10000
echo ============================================================
echo.

:loop
%PY% ml_botfarm.py %*
 set "EXIT=%errorlevel%"
 echo.
 echo ML bot farm exited with code %EXIT%.
 if %EXIT%==0 (
   echo Restarting bot farm in 5 seconds...
   timeout /t 5 /nobreak >nul
   goto loop
 ) else (
   echo Bot farm stopped.
 )
goto :eof