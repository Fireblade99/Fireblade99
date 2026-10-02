@echo off
setlocal
cd /d "%~dp0"

rem ===================================================================
rem  Creates 12 Grafana alert rules with curl. No PowerShell involved.
rem
rem  EDIT THE TWO LINES BELOW, then run this file.
rem
rem  Every rule body is a plain .json next to this script; curl sends
rem  the bytes as they are, so encoding settings cannot corrupt them.
rem
rem  Expected result for each line: HTTP 201. Anything else means the
rem  rule was not created - open the matching resp-NN.txt for details.
rem
rem  Safe to re-run ONLY after deleting what it already created:
rem  the API makes a new rule on every POST, it does not update.
rem ===================================================================

set "GRAFANA=http://your-grafana:3000"
set "TOKEN=glsa_put_your_token_here"

set "FOLDER=dfqsba5oqdgqof"

where curl.exe >nul 2>&1
if errorlevel 1 (
    echo curl.exe not found. It ships with Windows 10 1803 and later.
    exit /b 1
)

echo Grafana: %GRAFANA%
echo Folder:  %FOLDER%
echo.
echo [1/12] dwh-dbp2-lp2 / idle in transaction
curl -s -S -o "resp-01.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@01-dwh-dbp2-lp2-idle.json"
echo [2/12] dwh-dbp2-lp2 / long transaction
curl -s -S -o "resp-02.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@02-dwh-dbp2-lp2-longtx.json"
echo [3/12] dwh-dbp2-lp2 / lock waits
curl -s -S -o "resp-03.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@03-dwh-dbp2-lp2-locks.json"
echo [4/12] dwh-dbp3-lp1 / idle in transaction
curl -s -S -o "resp-04.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@04-dwh-dbp3-lp1-idle.json"
echo [5/12] dwh-dbp3-lp1 / long transaction
curl -s -S -o "resp-05.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@05-dwh-dbp3-lp1-longtx.json"
echo [6/12] dwh-dbp3-lp1 / lock waits
curl -s -S -o "resp-06.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@06-dwh-dbp3-lp1-locks.json"
echo [7/12] dwh-dbp10-lp2 / idle in transaction
curl -s -S -o "resp-07.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@07-dwh-dbp10-lp2-idle.json"
echo [8/12] dwh-dbp10-lp2 / long transaction
curl -s -S -o "resp-08.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@08-dwh-dbp10-lp2-longtx.json"
echo [9/12] dwh-dbp10-lp2 / lock waits
curl -s -S -o "resp-09.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@09-dwh-dbp10-lp2-locks.json"
echo [10/12] dwh-dbp12-lp2 / idle in transaction
curl -s -S -o "resp-10.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@10-dwh-dbp12-lp2-idle.json"
echo [11/12] dwh-dbp12-lp2 / long transaction
curl -s -S -o "resp-11.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@11-dwh-dbp12-lp2-longtx.json"
echo [12/12] dwh-dbp12-lp2 / lock waits
curl -s -S -o "resp-12.txt" -w "      HTTP %%{http_code}\n" -X POST "%GRAFANA%/api/v1/provisioning/alert-rules" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@12-dwh-dbp12-lp2-locks.json"

echo.
echo Setting evaluation interval for each group

echo   pg-prod-dp-dwh-dbp10-lp2
curl -s -S -o "resp-group-dwh-dbp10-lp2.txt" -w "      HTTP %%{http_code}\n" -X PUT "%GRAFANA%/api/v1/provisioning/folder/%FOLDER%/rule-groups/pg-prod-dp-dwh-dbp10-lp2" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@group-dwh-dbp10-lp2.json"
echo   pg-prod-dp-dwh-dbp12-lp2
curl -s -S -o "resp-group-dwh-dbp12-lp2.txt" -w "      HTTP %%{http_code}\n" -X PUT "%GRAFANA%/api/v1/provisioning/folder/%FOLDER%/rule-groups/pg-prod-dp-dwh-dbp12-lp2" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@group-dwh-dbp12-lp2.json"
echo   pg-prod-dp-dwh-dbp2-lp2
curl -s -S -o "resp-group-dwh-dbp2-lp2.txt" -w "      HTTP %%{http_code}\n" -X PUT "%GRAFANA%/api/v1/provisioning/folder/%FOLDER%/rule-groups/pg-prod-dp-dwh-dbp2-lp2" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@group-dwh-dbp2-lp2.json"
echo   pg-prod-dp-dwh-dbp3-lp1
curl -s -S -o "resp-group-dwh-dbp3-lp1.txt" -w "      HTTP %%{http_code}\n" -X PUT "%GRAFANA%/api/v1/provisioning/folder/%FOLDER%/rule-groups/pg-prod-dp-dwh-dbp3-lp1" -H "Authorization: Bearer %TOKEN%" -H "Content-Type: application/json; charset=utf-8" -H "X-Disable-Provenance: true" --data-binary "@group-dwh-dbp3-lp1.json"

echo.
echo Done. Every line above should read HTTP 201 (rules) or HTTP 200 (groups).
echo If any differs, open the matching resp-*.txt file.
echo.
echo Then in Grafana: Alerting -> Alert rules
echo   - check the new rules are not paused
echo   - delete the single hand-made rule in the BI folder
endlocal
