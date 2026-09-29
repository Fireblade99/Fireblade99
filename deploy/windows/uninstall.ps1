# Removes the scheduled tasks and firewall rule. Data in C:\qgw is kept (delete the folder manually).
param([int]$Port = 8080)
foreach ($n in "QlikGateway-API", "QlikGateway-Worker") {
    Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
}
Get-Process qlik-gateway -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-NetFirewallRule -DisplayName "Qlik Gateway $Port" -ErrorAction SilentlyContinue
Write-Host "Tasks and firewall rule removed."
