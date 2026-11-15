@echo off
rem Start the flux-meter service on port 8003, the port the units hard-code.
rem Double-click it, or run it from any folder; extra arguments are passed on (e.g. --fake).
rem The window stays open after the service stops, so an error can be read.
cd /d "%~dp0"
title flux-meter service :8003
".venv\Scripts\python.exe" -m flux_meter_service --port 8003 %*
echo.
echo The flux-meter service has stopped.
pause
