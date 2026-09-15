@echo off
setlocal EnableDelayedExpansion
title Text MMO - Torch Agent Batch Loop
cd /d "%~dp0"
set "PYTHONUNBUFFERED=1"

rem Run four Torch agents concurrently, wait for all four to finish, then
rem start four fresh characters. The server must already be running.
rem Usage: torch_batch_loop.bat [steps-per-agent] [rounds]
rem Default: 2000 training steps per agent.
rem Rounds default to 0 (run forever); use a positive value for a test/run limit.

where python >nul 2>nul
if %errorlevel%==0 ( set "PY=python" ) else ( set "PY=py" )

set "STEPS=%~1"
if "%STEPS%"=="" set "STEPS=2000"
set /a STEPS=STEPS 2>nul
if %STEPS% LSS 1 set STEPS=2000

set "ROUNDS=%~2"
if "%ROUNDS%"=="" set "ROUNDS=0"
set /a ROUNDS=ROUNDS 2>nul
if %ROUNDS% LSS 0 set ROUNDS=0

set "RUN_DIR=%TEMP%\textmmo_torch_batch_%RANDOM%"
if not exist "%RUN_DIR%" mkdir "%RUN_DIR%"

echo ============================================================
echo  Torch Agent Batch Loop
echo ============================================================
echo.
echo   Server:       ws://localhost:8765 (must be running first)
echo   Batch size:   4 concurrent agents
echo   Steps/agent:  %STEPS%
echo   Rounds:       %ROUNDS% (0 = forever)
echo   Logs/markers: %RUN_DIR%
echo.
echo   Each completed batch is replaced by four fresh characters.
echo   Press Ctrl+C to stop the loop and its current agents.
echo ============================================================
echo.

set /a ROUND=0

:next_batch
if %ROUNDS% GTR 0 if !ROUND! GEQ %ROUNDS% goto finished
set /a FIRST=ROUND*4
set /a LAST=FIRST+3
echo Starting batch !ROUND! (characters TorchBatch!ROUND!_0 through TorchBatch!ROUND!_3) ...

for /L %%i in (0,1,3) do (
    set "NAME=TorchBatch!ROUND!_%%i"
    set "DONE=!RUN_DIR!\!NAME!.done"
    set "LOG=!RUN_DIR!\!NAME!.log"
    del /q "!DONE!" 2>nul
    echo   Launching !NAME! ^> !LOG!
    start "!NAME!" cmd /d /c call "%~dp0torch_batch_worker.bat" "!NAME!" "%STEPS%" "!DONE!" >"!LOG!" 2>&1
)

:wait_batch
set /a FINISHED=0
for /L %%i in (0,1,3) do (
    set "NAME=TorchBatch!ROUND!_%%i"
    if exist "!RUN_DIR!\!NAME!.done" set /a FINISHED+=1
)
if !FINISHED! LSS 4 (
    timeout /t 5 /nobreak >nul
    goto wait_batch
)

echo Batch !ROUND! finished. Logs are in !RUN_DIR!.
set /a ROUND+=1
goto next_batch

:finished
echo Requested batch count reached. Logs are in !RUN_DIR!.
exit /b 0
