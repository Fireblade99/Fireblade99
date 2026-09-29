# Removes the scheduled tasks and firewall rule. Data in C:\qgw is kept (delete the folder manually).
param([int]$Port = 8080)
foreach ($n in "QlikGateway-API", "QlikGateway-Worker") {
    Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*qlik_gateway.cli*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
if (Get-Service qgw-postgresql -ErrorAction SilentlyContinue) {
    Write-Host "Portable PostgreSQL service 'qgw-postgresql' is kept. To remove: Stop-Service qgw-postgresql; C:\qgw\pgsql\bin\pg_ctl.exe unregister -N qgw-postgresql"
}
Remove-NetFirewallRule -DisplayName "Qlik Gateway $Port" -ErrorAction SilentlyContinue
Write-Host "Tasks and firewall rule removed."
