@echo off
setlocal
title Text MMO Engine + Live Dashboard
cd /d "%~dp0"

rem Verbose mode: prints the logs the engine would otherwise omit
rem (every client command, HTTP requests, connect/disconnect events).
set "TEXTMMO_VERBOSE=1"

where python >nul 2>nul
if %errorlevel%==0 ( set "PY=python" ) else ( set "PY=py" )

echo ============================================================
echo  Text MMO Engine + Live Dashboard
echo ============================================================
echo.
echo   Game server:    ws://localhost:8765
echo   Live dashboard: http://localhost:8766/
echo   GM stream:      ws://127.0.0.1:8767 (dashboard GM tab, loopback-only)
echo.
echo   Train the ML bot:    ml\ml_client.bat         (single agent)
echo                        ml\ml_botfarm.bat       (N concurrent bots)
echo                        torch_agents\torch_bot.bat      (PyTorch DQN, single agent)
echo                        torch_agents\torch_farm.bat      (PyTorch DQN, N agents, shared policy)
echo                        torch_agents\torch_batch_loop.bat (4 agents, repeat batches)
echo   GM console:          the dashboard's GM tab  (local, no auth)
echo.
echo   This window shows live logs (every command, HTTP hits,
echo   connections). Press Ctrl+C to stop the whole thing.
echo ============================================================
echo.

%PY% server.py

echo.
echo Server stopped (it may have crashed, or the port is in use).
pause
