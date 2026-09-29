@echo off
rem Started by the "QlikGateway-API" scheduled task. Settings are read from .env in this folder.
cd /d "%~dp0"
set PORT=8080
for /f "tokens=1,* delims==" %%a in ('findstr /b "QGW_API_PORT=" .env') do set PORT=%%b
"%~dp0venv\Scripts\qlik-gateway.exe" api --host 0.0.0.0 --port %PORT% --workers 1 >> "%~dp0logs\api.log" 2>&1
