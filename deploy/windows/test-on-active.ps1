<#
.SYNOPSIS
  Checks how the gateway collapses start requests (on_active) against a real Qlik task.

.DESCRIPTION
  Uses two API clients (two tokens = two "teams") and one Qlik task that both may start.
  1. The task is idle: A, B, A send a start right after each other -> one execution id for all
     (they collapse while the run waits in the gateway queue).
  2. Waits until that run is RELOADING in Qlik, then for each client:
       on_active=reuse  -> the active execution id + warning + link
       on_active=queue  -> one NEW execution for both clients (collapsed), starts after the active one
       on_active=reject -> 409 already_running + reason + active execution + link
       (no on_active)   -> reject, the default
  The task is reloaded twice (the active run and the queued one). Use a test task.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\qlik-gateway\deploy\windows\test-on-active.ps1 `
      -Gateway http://localhost:8080 -TokenA qgw_... -TokenB qgw_... -TaskId 462fc8e2-5add-4c14-b2b4-28171e890e66
#>
param(
    [string]$Gateway = "http://localhost:8080",
    [Parameter(Mandatory = $true)][string]$TokenA,
    [Parameter(Mandatory = $true)][string]$TokenB,
    [Parameter(Mandatory = $true)][string]$TaskId,
    [int]$WaitRunningSeconds = 120
)
$ErrorActionPreference = "Stop"
$api = "$($Gateway.TrimEnd('/'))/api/v1"
$script:failed = 0

function Start-Reload([string]$token, [string]$onActive, [string]$who) {
    $body = @{ meta = @{ test = "on_active"; who = $who } }
    if ($onActive) { $body.on_active = $onActive }
    $json = $body | ConvertTo-Json -Compress
    try {
        $r = Invoke-RestMethod -Method Post -Uri "$api/tasks/$TaskId/start" -Body $json `
            -ContentType "application/json" -Headers @{ Authorization = "Bearer $token" }
        $r | Add-Member -NotePropertyName http -NotePropertyValue 202 -PassThru
    } catch {
        $code = [int]$_.Exception.Response.StatusCode
        $msg = $_.ErrorDetails.Message
        if (-not $msg) { throw }
        $r = $msg | ConvertFrom-Json
        $r | Add-Member -NotePropertyName http -NotePropertyValue $code -PassThru
    }
}

function Get-State([string]$token, [int]$id) {
    Invoke-RestMethod -Uri "$api/executions/$id" -Headers @{ Authorization = "Bearer $token" }
}

function Check([string]$name, [bool]$ok, [string]$detail) {
    if ($ok) { Write-Host "  PASS  $name  $detail" -ForegroundColor Green }
    else { Write-Host "  FAIL  $name  $detail" -ForegroundColor Red; $script:failed++ }
}

function Show($r) {
    $run = $r.running_execution
    $line = "http=$($r.http) execution=$($r.execution_id) dedup=$($r.deduplicated) on_active=$($r.on_active)"
    if ($r.error) { $line += " error=$($r.error)" }
    if ($run) { $line += " | active=$($run.execution_id) $($run.status) own=$($run.own) url=$($run.url)" }
    foreach ($w in @($r.warnings)) { if ($w) { $line += " | warning=$($w.code)" } }
    Write-Host "        $line" -ForegroundColor DarkGray
}

Write-Host "Gateway $Gateway, task $TaskId"

# ---------------------------------------------------------------------------------------------
Write-Host "`n1. Task is idle: requests collapse into one execution, whoever sends them" -ForegroundColor Cyan
$a1 = Start-Reload $TokenA "" "A-1";       Show $a1
$b1 = Start-Reload $TokenB "reject" "B-1"; Show $b1
$a2 = Start-Reload $TokenA "queue" "A-2";  Show $a2
if ($a1.http -ne 202) {
    Write-Host "The first start was not accepted ($($a1.error): $($a1.message)). Is the task idle and allowed for client A?" -ForegroundColor Red
    exit 1
}
$first = [int]$a1.execution_id
if ($a1.deduplicated) {
    Write-Host "  note: the task already had a queued run ($first); the test continues with it" -ForegroundColor Yellow
}
Check "1. client B gets the same id" ($b1.http -eq 202 -and [int]$b1.execution_id -eq $first) "B=$($b1.execution_id) A=$first"
Check "1. second request of A gets the same id" ($a2.http -eq 202 -and [int]$a2.execution_id -eq $first) "A2=$($a2.execution_id)"
Check "1. link to the execution" ([string]$a1.url -like "*/ui/executions/$first") "$($a1.url)"

# ---------------------------------------------------------------------------------------------
Write-Host "`n   waiting until execution $first is reloading in Qlik ..." -ForegroundColor DarkGray
$deadline = (Get-Date).AddSeconds($WaitRunningSeconds)
do {
    Start-Sleep -Seconds 2
    $st = Get-State $TokenA $first
} while ($st.status -eq "QUEUED" -and (Get-Date) -lt $deadline)
if ($st.status -notin @("STARTING", "RUNNING")) {
    Write-Host "  execution $first is $($st.status), not reloading - the task finished too fast or did not start. Use a task that reloads 30+ s." -ForegroundColor Red
    exit 1
}
Write-Host "   execution $first is $($st.status)"

# ---------------------------------------------------------------------------------------------
Write-Host "`n2. Task is reloading: on_active decides" -ForegroundColor Cyan
Write-Host "   reuse"
$ra = Start-Reload $TokenA "reuse" "A-reuse"; Show $ra
$rb = Start-Reload $TokenB "reuse" "B-reuse"; Show $rb
foreach ($p in @(@("A", $ra), @("B", $rb))) {
    $r = $p[1]
    Check "2. reuse ($($p[0])) -> active id" ($r.http -eq 202 -and [int]$r.execution_id -eq $first) "got $($r.execution_id)"
    Check "2. reuse ($($p[0])) -> info + link" ([string]$r.running_execution.url -like "*/ui/executions/$first") "$($r.running_execution.url)"
    Check "2. reuse ($($p[0])) -> warning" ((@($r.warnings) | ForEach-Object { $_.code }) -contains "reused_active_run") ""
}
Check "2. reuse: B does not see A's initiator" (-not $rb.running_execution.own -and -not $rb.running_execution.client) "own=$($rb.running_execution.own)"

Write-Host "   reject (explicit and default)"
$xa = Start-Reload $TokenA "reject" "A-reject"; Show $xa
$xb = Start-Reload $TokenB "" "B-default";     Show $xb
foreach ($p in @(@("A explicit", $xa), @("B default", $xb))) {
    $r = $p[1]
    Check "2. reject ($($p[0])) -> 409 already_running" ($r.http -eq 409 -and $r.error -eq "already_running") "http=$($r.http) $($r.error)"
    Check "2. reject ($($p[0])) -> reason" ([string]$r.message -ne "") "$($r.message)"
    Check "2. reject ($($p[0])) -> active run + link" ([int]$r.running_execution.execution_id -eq $first -and [string]$r.running_execution.url -like "*/ui/executions/$first") "$($r.running_execution.url)"
}

Write-Host "   queue"
$qa = Start-Reload $TokenA "queue" "A-queue"; Show $qa
$qb = Start-Reload $TokenB "queue" "B-queue"; Show $qb
$queued = [int]$qa.execution_id
Check "2. queue (A) -> new execution after the active one" ($qa.http -eq 202 -and $queued -ne $first -and -not $qa.deduplicated) "new=$queued active=$first"
Check "2. queue (B) -> collapsed into the same queued run" ($qb.http -eq 202 -and [int]$qb.execution_id -eq $queued) "B=$($qb.execution_id)"
$qs = Get-State $TokenA $queued
Check "2. queued run waits for the active one" ($qs.status -eq "QUEUED") "status=$($qs.status)"

# ---------------------------------------------------------------------------------------------
Write-Host ""
if ($script:failed) {
    Write-Host "$($script:failed) check(s) FAILED" -ForegroundColor Red
    exit 1
}
Write-Host "All checks passed. Execution $queued will reload the task once more after $first; see $Gateway/ui/executions" -ForegroundColor Green
