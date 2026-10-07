# Restarts both gateway tasks (run as Administrator), e.g. after editing .env
param([string]$InstallDir = "C:\qgw", [int]$Port = 8080)
$ErrorActionPreference = "Stop"

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

$tasks = "QlikGateway-API", "QlikGateway-Worker"
# the tasks restart a stopped gateway by themselves: switch them off while stopping
foreach ($n in $tasks) {
    Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue | Out-Null
}
try { Stop-Gateway }
finally { foreach ($n in $tasks) { Enable-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue | Out-Null } }
foreach ($n in $tasks) { Start-ScheduledTask -TaskName $n; Write-Host "$n started" }
