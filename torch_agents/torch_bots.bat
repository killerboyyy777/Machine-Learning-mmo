@echo off
setlocal EnableDelayedExpansion
title Text MMO - Torch Bots
cd /d "%~dp0"
set "PYTHONUNBUFFERED=1"

rem PyTorch DQN launcher: starts N concurrent torch bots
rem Usage: torch_bots.bat [--count N] | [N]
rem   default count: 4 bots

rem Determine Python interpreter
where python >nul 2>nul
if %errorlevel%==0 ( set "PY=python" ) else ( set "PY=py" )

rem Parse count argument (must be a positive number, else default 4)
set COUNT=4
if "%~1"=="--count" if not "%~2"=="" set COUNT=%~2
if "%~1" neq "" if not "%~1"=="--count" set COUNT=%~1
set /a COUNT=%COUNT% 2>nul
if %COUNT% LSS 1 set COUNT=1

echo ============================================================
echo  Starting %COUNT% PyTorch DQN Bots for Text MMO
echo ============================================================
echo.
echo   Server:   ws://localhost:8765 (must be running first)
echo   Bots:     %COUNT% concurrent agents with unique names
echo   Names:    TorchBot0, TorchBot1, ...
echo.
echo   Each bot runs TorchDQNAgent with auxiliary gold/loot/market/quest heads.
echo   Press Ctrl+C to stop this launcher (bots will continue running).
echo.
echo ============================================================
echo.

rem Launch the bots (0 .. COUNT-1)
set /a LAST=%COUNT%-1
for /L %%i in (0,1,%LAST%) do (
    set "bot_name=TorchBot%%i"
    echo Launching !bot_name! in background ...
    start "TorchBot%%i" %PY% dqn_agent.py --name "!bot_name!"
)

echo.
echo All %COUNT% bots launched in background.
echo Check the server dashboard at http://localhost:8766/ for activity.
echo.
echo To stop all bots, close this window or press Ctrl+C.
echo The bots will continue running until their training completes.

pause