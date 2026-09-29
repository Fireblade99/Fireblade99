@echo off
rem Restart both services (run as Administrator), e.g. after editing .env
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart.ps1"
