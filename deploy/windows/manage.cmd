@echo off
rem Admin CLI, e.g.:  manage.cmd create-admin admin
rem                   manage.cmd create-client airflow-prod --tasks *
cd /d "%~dp0"
"%~dp0venv\Scripts\qlik-gateway.exe" %*
