@echo off
setlocal
title Text MMO - Shared Torch Farm
cd /d "%~dp0"
set "PYTHONUNBUFFERED=1"

where python >nul 2>nul
if %errorlevel%==0 ( set "PY=python" ) else ( set "PY=py" )

echo ============================================================
echo  Shared Torch DQN Farm
echo ============================================================
echo   Four agents share one policy and one checkpoint writer.
echo   Server: ws://localhost:8765 (must be running first)
echo   Usage:  torch_farm.bat [--agents 4 --steps 1000000]
echo ============================================================
echo.

%PY% torch_farm.py %*
set "EXIT=%errorlevel%"
echo.
echo Torch farm stopped with code %EXIT%.
pause
