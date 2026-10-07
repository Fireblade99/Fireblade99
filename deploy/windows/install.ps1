<#
.SYNOPSIS
  Installs Qlik Gateway on Windows Server: venv, packages (via Nexus/PyPI), .env, two auto-start tasks.

.EXAMPLE
  # from the unpacked project folder, in an elevated PowerShell.
  # Installs PostgreSQL on this node from the EDB installer and creates the database:
  powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 `
      -IndexUrl https://nexus.company.local/repository/pypi-proxy/simple `
      -PgInstaller C:\distr\postgresql-16.4-1-windows-x64.exe -PgDataDir D:\pgdata

.EXAMPLE
  # use an existing PostgreSQL instead:
  ... install.ps1 -IndexUrl ... -DatabaseUrl "postgresql+psycopg://qgw:secret@pg-host:5432/qlik_gateway"

.NOTES
  Services are registered as Scheduled Tasks (built into Windows, nothing to download):
  "QlikGateway-API" and "QlikGateway-Worker", run as SYSTEM at startup, restarted on failure.
#>
param(
    [string]$InstallDir = "C:\qgw",
    [string]$IndexUrl = "",            # Nexus PyPI proxy, e.g. https://nexus/repository/pypi-proxy/simple
    [string]$TrustedHost = "",         # set to the Nexus host if its certificate is not trusted by pip
    [string]$Python = "",              # path to python.exe (3.10+); autodetected if empty
    [string]$PythonPackage = "",       # portable Python, no installer: python.3.12.x.nupkg from nuget.org (or its .zip)
    [int]$Port = 8080,
    [string]$DatabaseUrl = "",         # existing PostgreSQL (postgresql+psycopg://user:pass@host:5432/db)
    [string]$PgInstaller = "",         # EDB PostgreSQL installer .exe -> installs PostgreSQL on this node
    [string]$PgZip = "",               # portable PostgreSQL, no installer: EDB "binaries" zip (postgresql-16.x-windows-x64-binaries.zip)
    [string]$PgDataDir = "C:\pgdata",  # data directory for the local PostgreSQL (put it on the big disk)
    [string]$PgPrefix = "C:\Program Files\PostgreSQL\16",
    [int]$PgPort = 5432,
    [switch]$NoTasks                   # only install, do not register/start the tasks
)
$ErrorActionPreference = "Stop"
function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

$src = Resolve-Path (Join-Path $PSScriptRoot "..\..")
if (-not (Test-Path (Join-Path $src "pyproject.toml"))) { throw "pyproject.toml not found in $src" }

Step "Folders in $InstallDir"
foreach ($d in @("", "app", "data", "logs", "secrets")) { New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir $d) | Out-Null }
# copy the project (without tests/.git) so the install does not depend on where the archive was unpacked
robocopy $src (Join-Path $InstallDir "app") /MIR /XD .git tests __pycache__ venv .venv /XF *.db .env /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }

function Expand-AnyArchive($archive, $dest) {
    # Expand-Archive only accepts *.zip, a .nupkg is a zip as well
    $tmp = Join-Path $env:TEMP ("qgw_" + [guid]::NewGuid().ToString("N") + ".zip")
    Copy-Item $archive $tmp
    Expand-Archive -Path $tmp -DestinationPath $dest -Force
    Remove-Item $tmp
}

Step "Python"
if ($PythonPackage) {
    # Portable Python: no MSI, works where installers are blocked by policy. Used directly, without a venv.
    $pyDir = Join-Path $InstallDir "python"
    if (-not (Test-Path (Join-Path $pyDir "python.exe"))) {
        if (-not (Test-Path $PythonPackage)) { throw "Python package not found: $PythonPackage" }
        $tmpDir = Join-Path $env:TEMP ("qgw_py_" + [guid]::NewGuid().ToString("N"))
        Expand-AnyArchive $PythonPackage $tmpDir
        $exe = Get-ChildItem $tmpDir -Recurse -Filter python.exe | Select-Object -First 1
        if (-not $exe) { throw "python.exe not found inside $PythonPackage" }
        New-Item -ItemType Directory -Force -Path $pyDir | Out-Null
        Copy-Item (Join-Path $exe.DirectoryName "*") $pyDir -Recurse -Force
        Remove-Item $tmpDir -Recurse -Force
    }
    $Py = Join-Path $pyDir "python.exe"
    & $Py -m pip --version *> $null
    if ($LASTEXITCODE) { & $Py -m ensurepip --default-pip; if ($LASTEXITCODE) { throw "ensurepip failed" } }
} else {
    if (-not $Python) {
        $cmd = Get-Command py -ErrorAction SilentlyContinue
        if ($cmd) { $Python = (& py -3 -c "import sys; print(sys.executable)").Trim() }
        else {
            $cmd = Get-Command python -ErrorAction SilentlyContinue
            if (-not $cmd) { throw "Python 3.10+ not found. Install it, or use -PythonPackage <python.3.12.x.nupkg> if installers are blocked" }
            $Python = $cmd.Source
        }
    }
    $venv = Join-Path $InstallDir "venv"
    if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) { & $Python -m venv $venv; if ($LASTEXITCODE) { throw "venv failed" } }
    $Py = Join-Path $venv "Scripts\python.exe"
}
$ver = & $Py -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "Using $Py ($ver)"
if ([version]$ver -lt [version]"3.10") { throw "Python 3.10+ required, found $ver" }

Step "Stopping a running gateway (if any)"
# Otherwise the old processes keep port 8080 and serve the old code after the update.
foreach ($n in "QlikGateway-API", "QlikGateway-Worker") { Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*qlik_gateway.cli*" } |
    ForEach-Object { Write-Host "stopping PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Step "Packages"
$pip = @("-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location")
if ($IndexUrl) { $pip += @("--index-url", $IndexUrl) }
if ($TrustedHost) { $pip += @("--trusted-host", $TrustedHost) }
& $Py @pip --upgrade pip
if ($LASTEXITCODE) { throw "pip upgrade failed (check -IndexUrl / -TrustedHost)" }
& $Py @pip ((Join-Path $InstallDir "app") + "[postgres]")
if ($LASTEXITCODE) { throw "package install failed" }
# pip skips a local package whose version did not change: always put the new gateway code in place
& $Py @pip --force-reinstall --no-deps (Join-Path $InstallDir "app")
if ($LASTEXITCODE) { throw "gateway code reinstall failed" }

Step "Launch scripts"
# -P: do not put the working folder on sys.path, so a stray copy of the sources in the
# install folder (e.g. C:\qgw\qlik_gateway) can never shadow the installed package.
if ([version]$ver -lt [version]"3.11") { throw "Python 3.11+ required for the launch scripts (-P)" }
foreach ($stray in "qlik_gateway", "deploy", "tests") {
    $p = Join-Path $InstallDir $stray
    if (Test-Path $p) { Write-Warning "$p is a stray copy of the sources and is not used; delete it" }
}
$runApi = @"
@echo off
rem Started by the "QlikGateway-API" scheduled task. Settings are read from .env in this folder.
cd /d "%~dp0"
"$Py" -P -m qlik_gateway.cli api --host 0.0.0.0 --port $Port --workers 1 >> "%~dp0logs\api.log" 2>&1
"@
$runWorker = @"
@echo off
rem Started by the "QlikGateway-Worker" scheduled task: the single coordinator that talks to Qlik.
cd /d "%~dp0"
"$Py" -P -m qlik_gateway.cli worker >> "%~dp0logs\worker.log" 2>&1
"@
$manage = @"
@echo off
rem Admin CLI, e.g.:  manage.cmd create-admin admin, manage.cmd create-client airflow-prod --tasks *
cd /d "%~dp0"
"$Py" -P -m qlik_gateway.cli %*
"@
Set-Content (Join-Path $InstallDir "run-api.cmd") $runApi -Encoding ascii
Set-Content (Join-Path $InstallDir "run-worker.cmd") $runWorker -Encoding ascii
Set-Content (Join-Path $InstallDir "manage.cmd") $manage -Encoding ascii
Copy-Item (Join-Path $PSScriptRoot "restart.ps1"), (Join-Path $PSScriptRoot "restart.cmd") $InstallDir -Force

function New-Password([int]$n = 24) { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count $n | ForEach-Object { [char]$_ }) }

if ($PgZip) {
    Step "Portable PostgreSQL (no installer)"
    $PgPrefix = Join-Path $InstallDir "pgsql"
    $superFile = Join-Path $InstallDir "secrets\postgres_superuser.txt"
    if (-not (Test-Path (Join-Path $PgPrefix "bin\pg_ctl.exe"))) {
        if (-not (Test-Path $PgZip)) { throw "PostgreSQL zip not found: $PgZip" }
        Expand-AnyArchive $PgZip $InstallDir      # the EDB zip contains a top-level "pgsql" folder
        if (-not (Test-Path (Join-Path $PgPrefix "bin\pg_ctl.exe"))) { throw "pgsql\bin\pg_ctl.exe not found after unpacking $PgZip" }
    }
    if (-not (Test-Path (Join-Path $PgDataDir "PG_VERSION"))) {
        if ((Test-Path $PgDataDir) -and (Get-ChildItem $PgDataDir -Force | Select-Object -First 1)) {
            throw "$PgDataDir is not empty but has no database in it (a failed earlier attempt?). Empty or delete the folder and re-run"
        }
        $super = New-Password
        Set-Content -Path $superFile -Value $super -Encoding ascii
        $pwFile = Join-Path $env:TEMP "qgw_pw.txt"; Set-Content $pwFile $super -Encoding ascii
        New-Item -ItemType Directory -Force -Path $PgDataDir | Out-Null
        # initdb drops administrator rights before touching the data directory, so the plain
        # current user must own it (a folder created from an elevated shell belongs to Administrators)
        $me = "$env:USERDOMAIN\$env:USERNAME"
        icacls $PgDataDir /setowner $me /T /Q | Out-Null
        icacls $PgDataDir /grant "${me}:(OI)(CI)F" /T /Q | Out-Null
        icacls $pwFile /grant "${me}:R" /Q | Out-Null
        & (Join-Path $PgPrefix "bin\initdb.exe") -D $PgDataDir -U postgres -E UTF8 --locale=C --auth=scram-sha-256 "--pwfile=$pwFile"
        $rc = $LASTEXITCODE; Remove-Item $pwFile
        if ($rc) { throw "initdb failed ($rc), see the message above. VCRUNTIME140.dll missing = install the Visual C++ 2015-2022 runtime. Before re-running, empty $PgDataDir" }
        Write-Host "postgres superuser password saved to $superFile"
    }
    # the service runs as NETWORK SERVICE; give it the data directory
    icacls $PgDataDir /grant "NT AUTHORITY\NetworkService:(OI)(CI)F" /T /Q | Out-Null
    if (-not (Get-Service qgw-postgresql -ErrorAction SilentlyContinue)) {
        & (Join-Path $PgPrefix "bin\pg_ctl.exe") register -N qgw-postgresql -D $PgDataDir -S auto -o "-p $PgPort"
        if ($LASTEXITCODE) { throw "pg_ctl register failed" }
    }
    Start-Service qgw-postgresql
    Start-Sleep -Seconds 3
    $PgInstaller = "__portable__"   # reuse the database/role creation below
}

if ($PgInstaller) {
    Step "PostgreSQL database"
    $psql = Join-Path $PgPrefix "bin\psql.exe"
    $superFile = Join-Path $InstallDir "secrets\postgres_superuser.txt"
    if (-not (Test-Path $psql) -and $PgInstaller -ne "__portable__") {
        if (-not (Test-Path $PgInstaller)) { throw "PostgreSQL installer not found: $PgInstaller" }
        $super = New-Password
        New-Item -ItemType Directory -Force -Path $PgDataDir | Out-Null
        $pgArgs = @("--mode", "unattended", "--unattendedmodeui", "none", "--superpassword", $super,
                  "--serverport", "$PgPort", "--prefix", "`"$PgPrefix`"", "--datadir", "`"$PgDataDir`"",
                  "--enable-components", "server,commandlinetools", "--disable-components", "pgAdmin,stackbuilder")
        Write-Host "Installing PostgreSQL (takes a few minutes)..."
        $p = Start-Process -FilePath $PgInstaller -ArgumentList $pgArgs -Wait -PassThru
        if ($p.ExitCode) { throw "PostgreSQL installer failed with code $($p.ExitCode)" }
        Set-Content -Path $superFile -Value $super -Encoding ascii
        Write-Host "postgres superuser password saved to $superFile"
    } else {
        Write-Host "PostgreSQL already installed in $PgPrefix"
        if (-not (Test-Path $superFile)) { throw "Existing PostgreSQL: put the postgres superuser password into $superFile and re-run" }
        $super = (Get-Content $superFile -Raw).Trim()
    }
    $env:PGPASSWORD = $super
    $exists = & $psql -h localhost -p $PgPort -U postgres -tAc "select 1 from pg_roles where rolname='qgw'"
    if ($LASTEXITCODE) { throw "Cannot connect to PostgreSQL on localhost:$PgPort" }
    $dbPass = New-Password
    if ($exists -match "1") {
        & $psql -h localhost -p $PgPort -U postgres -c "alter role qgw with login password '$dbPass'" | Out-Null
    } else {
        & $psql -h localhost -p $PgPort -U postgres -c "create role qgw with login password '$dbPass'" | Out-Null
    }
    $hasDb = & $psql -h localhost -p $PgPort -U postgres -tAc "select 1 from pg_database where datname='qlik_gateway'"
    if (-not ($hasDb -match "1")) { & $psql -h localhost -p $PgPort -U postgres -c "create database qlik_gateway owner qgw" | Out-Null }
    Remove-Item Env:PGPASSWORD
    $DatabaseUrl = "postgresql+psycopg://qgw:$dbPass@localhost:$PgPort/qlik_gateway"
    Write-Host "Database qlik_gateway / role qgw ready"
}

Step ".env"
$envFile = Join-Path $InstallDir ".env"
if (Test-Path $envFile) {
    if ($DatabaseUrl) {
        (Get-Content $envFile) -replace "^QGW_DATABASE_URL=.*", "QGW_DATABASE_URL=$DatabaseUrl" | Set-Content $envFile -Encoding ascii
        Write-Host ".env exists: only QGW_DATABASE_URL updated"
    } else {
        Write-Host ".env already exists, left untouched"
    }
} else {
    $bytes = New-Object byte[] 48; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $secret = [Convert]::ToBase64String($bytes)
    $adminPass = New-Password 16
    $root = $InstallDir -replace "\\", "/"
    if (-not $DatabaseUrl) { $DatabaseUrl = "sqlite:///$root/data/qlik_gateway.db" }
    @"
# ---- web / UI ----
QGW_SECRET_KEY=$secret
QGW_SESSION_HTTPS_ONLY=false
QGW_UI_UTC_OFFSET_HOURS=5
QGW_BOOTSTRAP_ADMIN_USER=admin
QGW_BOOTSTRAP_ADMIN_PASSWORD=$adminPass

# ---- storage ----
QGW_DATABASE_URL=$DatabaseUrl

# ---- Qlik: mock = built-in emulator. Switch to jwt after docs/qlik-setup.md is done ----
QGW_QLIK_MODE=mock
QGW_QLIK_BASE_URL=https://qlik.company.local/airflowgw
QGW_QLIK_VERIFY_SSL=system
QGW_QLIK_JWT_PRIVATE_KEY_PATH=$root/secrets/qlik_jwt_private.pem
QGW_QLIK_JWT_USER_ID=svc_qlik_gateway
QGW_QLIK_JWT_USER_DIRECTORY=CORP
QGW_QLIK_TASK_CUSTOM_PROPERTY=ExternalRun
QGW_QLIK_TASK_CUSTOM_PROPERTY_VALUE=Yes
QGW_QLIK_CLIENT_CUSTOM_PROPERTY=GatewayClient
QGW_NODE_HEALTH_URLS=

# ---- coordinator ----
QGW_MAX_CONCURRENT_EXECUTIONS=3
QGW_POLL_INTERVAL_SECONDS=20
QGW_API_PORT=$Port
"@ | Set-Content -Path $envFile -Encoding ascii
    Write-Host "Created $envFile"
    Write-Host "UI admin login: admin / $adminPass   (change it later: $InstallDir\manage.cmd create-admin admin)" -ForegroundColor Yellow
}
# the .env holds secrets: only Administrators and SYSTEM may read it
icacls $envFile /inheritance:r /grant:r "Administrators:F" "SYSTEM:F" | Out-Null
icacls (Join-Path $InstallDir "secrets") /inheritance:r /grant:r "Administrators:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null

if ($NoTasks) { Write-Host "`nInstalled. Tasks not registered (-NoTasks)."; exit 0 }

Step "Auto-start tasks"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$trigger = New-ScheduledTaskTrigger -AtStartup
foreach ($t in @(@{Name = "QlikGateway-API"; Cmd = "run-api.cmd" }, @{Name = "QlikGateway-Worker"; Cmd = "run-worker.cmd" })) {
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$InstallDir\$($t.Cmd)`"" -WorkingDirectory $InstallDir
    Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
    Start-ScheduledTask -TaskName $t.Name
    Write-Host "Task $($t.Name) registered and started"
}

Step "Firewall: allow inbound TCP $Port"
if (-not (Get-NetFirewallRule -DisplayName "Qlik Gateway $Port" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "Qlik Gateway $Port" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
}

Step "Health check"
Start-Sleep -Seconds 8
try {
    $r = Invoke-RestMethod "http://localhost:$Port/healthz" -TimeoutSec 10
    Write-Host "OK: $($r | ConvertTo-Json -Compress)" -ForegroundColor Green
    Write-Host "UI: http://$(hostname):$Port/ui/"
} catch {
    Write-Warning "Gateway did not answer yet. Check $InstallDir\logs\api.log and worker.log"
}
