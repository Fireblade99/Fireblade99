<#
.SYNOPSIS
  Quick update of the gateway code only. .env, the database, Python and PostgreSQL are not touched.

.EXAMPLE
  # unpack the new archive over C:\qlik-gateway, then (as Administrator):
  powershell -ExecutionPolicy Bypass -File C:\qlik-gateway\deploy\windows\update.ps1 -IndexUrl https://nexus/repository/pypi-proxy/simple
#>
param(
    [string]$Src = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$IndexUrl = "",
    [string]$TrustedHost = "",
    [string]$InstallDir = "C:\qgw",
    [int]$Port = 8080
)
$ErrorActionPreference = "Stop"
$py = Get-ChildItem (Join-Path $InstallDir "python\python.exe"), (Join-Path $InstallDir "venv\Scripts\python.exe") -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $py) { throw "Python of the gateway not found in $InstallDir" }

function Stop-Gateway {
    # every gateway process, however it was started (task, run-api.cmd in a window, another user):
    # python.exe of this installation, anything running qlik_gateway, and whatever listens on the port
    $pyDir = Join-Path $InstallDir "python"
    $ids = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object {
            $_.CommandLine -like "*qlik_gateway*" -or ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($pyDir, "OrdinalIgnoreCase"))
        } | ForEach-Object { $_.ProcessId })
    $ids += @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $_.OwningProcess })
    foreach ($id in ($ids | Where-Object { $_ -and $_ -ne 0 -and $_ -ne 4 } | Sort-Object -Unique)) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 10 -and (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue); $i++) {
        Start-Sleep -Seconds 1
    }
    $left = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($left) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($left[0].OwningProcess)"
        throw "Port $Port is still taken by process $($p.ProcessId): $($p.ExecutablePath) $($p.CommandLine)"
    }
}


Write-Host "==> stop"
foreach ($n in "QlikGateway-API", "QlikGateway-Worker") { Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue }
Stop-Gateway

Write-Host "==> install code from $Src"
$idx = @()
if ($IndexUrl) { $idx += @("--index-url", $IndexUrl) }
if ($TrustedHost) { $idx += @("--trusted-host", $TrustedHost) }
# 1. new dependencies of this version, if any (already installed ones are kept)
& $py -m pip install --disable-pip-version-check --no-warn-script-location @idx $Src
if ($LASTEXITCODE) { throw "pip failed (check -IndexUrl / -TrustedHost)" }
# 2. the gateway code itself, even if the version number did not change
& $py -m pip install --force-reinstall --no-deps --disable-pip-version-check --no-warn-script-location @idx $Src
if ($LASTEXITCODE) { throw "pip failed (check -IndexUrl / -TrustedHost)" }

Write-Host "==> start"
foreach ($n in "QlikGateway-API", "QlikGateway-Worker") { Start-ScheduledTask -TaskName $n }
# the API needs a few seconds (more on a busy server): wait up to 60 s, then show the log instead of failing
$v = $null
Write-Host -NoNewline "waiting for the gateway on port $Port "
for ($i = 0; $i -lt 30 -and -not $v; $i++) {
    Start-Sleep -Seconds 2
    Write-Host -NoNewline "."
    try {
        $v = ((Invoke-WebRequest "http://localhost:$Port/openapi.json" -UseBasicParsing -TimeoutSec 3).Content |
            Select-String '"version":"[^"]*"').Matches.Value
    } catch { }
}
Write-Host ""
$installed = (& $py -P -c "import qlik_gateway; print(qlik_gateway.__version__)").Trim()
if ($v -and $v -notlike "*$installed*") {
    $p = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 |
        ForEach-Object { Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" }
    Write-Warning "Port $Port answers $v, but $installed is installed: an old gateway process is still serving."
    if ($p) { Write-Warning "It is process $($p.ProcessId): $($p.ExecutablePath) $($p.CommandLine)" }
    Write-Warning "Run C:\qgw\restart.cmd (as Administrator); if it stays, stop that process and run it again."
    exit 1
}
if ($v) {
    Write-Host "running: $v" -ForegroundColor Green
} else {
    Write-Warning "The gateway did not answer on port $Port within 60 s. Last lines of $InstallDir\logs\api.log:"
    Get-Content (Join-Path $InstallDir "logs\api.log") -Tail 40 -ErrorAction SilentlyContinue
    exit 1
}
