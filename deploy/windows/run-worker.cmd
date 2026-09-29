@echo off
rem Started by the "QlikGateway-Worker" scheduled task: the single coordinator that talks to Qlik.
cd /d "%~dp0"
"%~dp0venv\Scripts\qlik-gateway.exe" worker >> "%~dp0logs\worker.log" 2>&1
