@echo off
setlocal

rem Worker for torch_batch_loop.bat. Always writes the marker, even when the
rem agent exits with an error, so the parent can advance or report the log.
set "NAME=%~1"
set "STEPS=%~2"
set "DONE=%~3"

where python >nul 2>nul
if %errorlevel%==0 ( set "PY=python" ) else ( set "PY=py" )

%PY% dqn_agent.py --name "%NAME%" --steps "%STEPS%"
set "EXIT=%errorlevel%"
>"%DONE%" echo %EXIT%
exit /b %EXIT%
