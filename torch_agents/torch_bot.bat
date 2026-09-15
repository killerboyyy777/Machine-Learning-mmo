@echo off
setlocal
title Text MMO - Torch DQN Agent
cd /d "%~dp0"
set "PYTHONUNBUFFERED=1"

rem PyTorch DQN agent for the text MMO
where python >nul 2>nul
if %errorlevel%==0 ( set "PY=python" ) else ( set "PY=py" )

echo ============================================================
echo  PyTorch DQN Agent for Text MMO
echo ============================================================
echo.
echo   Agent:    torch_agents/dqn_agent.py (PyTorch DQN)
echo   Server:   ws://localhost:8765 (must be running)
echo.
echo   This agent learns via the server's score reward signal,
echo   inheriting the game's anti-grind variety/diminishing-returns curve.
echo.
echo   Requires: server running (..\start.bat) and PyTorch installed.
echo   Usage:    torch_bot.bat [--name MyAgent --steps 5000]
echo             torch_bot.bat --demo   (quick smoke test, no training)
echo.
echo ============================================================

%PY% dqn_agent.py %*
set "EXIT=%errorlevel%"
echo.
echo Agent stopped with code %EXIT%.
pause