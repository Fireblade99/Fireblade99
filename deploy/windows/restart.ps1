# Restarts both gateway tasks (run as Administrator), e.g. after editing .env
foreach ($n in "QlikGateway-API", "QlikGateway-Worker") { Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*qlik_gateway.cli*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
foreach ($n in "QlikGateway-API", "QlikGateway-Worker") { Start-ScheduledTask -TaskName $n; Write-Host "$n started" }
