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

Write-Host "==> stop"
foreach ($n in "QlikGateway-API", "QlikGateway-Worker") { Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*qlik_gateway*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Write-Host "==> install code from $Src"
$idx = @()
if ($IndexUrl) { $idx += @("--index-url", $IndexUrl) }
if ($TrustedHost) { $idx += @("--trusted-host", $TrustedHost) }
# 1. new dependencies of this version, if any (already installed ones are kept)
& $py -m pip install --disable-pip-version-check @idx $Src
if ($LASTEXITCODE) { throw "pip failed (check -IndexUrl / -TrustedHost)" }
# 2. the gateway code itself, even if the version number did not change
& $py -m pip install --force-reinstall --no-deps --disable-pip-version-check @idx $Src
if ($LASTEXITCODE) { throw "pip failed (check -IndexUrl / -TrustedHost)" }

Write-Host "==> start"
foreach ($n in "QlikGateway-API", "QlikGateway-Worker") { Start-ScheduledTask -TaskName $n }
# the API needs a few seconds (more on a busy server): wait up to 60 s, then show the log instead of failing
$v = $null
for ($i = 0; $i -lt 30 -and -not $v; $i++) {
    Start-Sleep -Seconds 2
    try {
        $v = ((Invoke-WebRequest "http://localhost:$Port/openapi.json" -UseBasicParsing -TimeoutSec 5).Content |
            Select-String '"version":"[^"]*"').Matches.Value
    } catch { }
}
if ($v) {
    Write-Host "running: $v" -ForegroundColor Green
} else {
    Write-Warning "The gateway did not answer on port $Port within 60 s. Last lines of $InstallDir\logs\api.log:"
    Get-Content (Join-Path $InstallDir "logs\api.log") -Tail 40 -ErrorAction SilentlyContinue
    exit 1
}
