@echo off
rem Restart both services (run as Administrator), e.g. after editing .env
schtasks /End /TN QlikGateway-API >nul 2>&1
schtasks /End /TN QlikGateway-Worker >nul 2>&1
taskkill /F /FI "IMAGENAME eq qlik-gateway.exe" >nul 2>&1
timeout /t 3 /nobreak >nul
schtasks /Run /TN QlikGateway-API
schtasks /Run /TN QlikGateway-Worker
