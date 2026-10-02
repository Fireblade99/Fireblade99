<#
.SYNOPSIS
    Creates Grafana alert rules that query PostgreSQL directly.

.DESCRIPTION
    This file is deliberately pure ASCII. All Russian text, all SQL and all
    thresholds live in pg-sql-alerts.json next to it, and that file is read
    with an explicitly specified UTF-8 encoding.

    Reason: Windows PowerShell 5.1 reads .ps1 in the system ANSI codepage
    unless the file carries a BOM, and a BOM is easily lost by a download,
    an editor or an antivirus. Keeping the script ASCII removes that whole
    class of failure - the script cannot be mis-decoded, and the data file
    is never decoded by PowerShell's file reader at all.

.EXAMPLE
    .\Import-PgSqlAlerts.ps1 -GrafanaUrl http://grafana:3000 -Token glsa_xxx -DryRun
    .\Import-PgSqlAlerts.ps1 -GrafanaUrl http://grafana:3000 -Token glsa_xxx
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $GrafanaUrl,
    [Parameter(Mandatory)] [string] $Token,
    [string] $Config,
    [string] $Folder,
    [string] $ContactPoint,
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$GrafanaUrl = $GrafanaUrl.TrimEnd('/')

# Rule titles printed below come from the JSON and are in Russian.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Step { param($m) Write-Host "==> $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "    $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------
# Load the data file.
#
# ReadAllText with an explicit encoding, not Get-Content: Get-Content
# would fall back to the system codepage, which is exactly what we are
# avoiding. A stray BOM is trimmed by hand because .NET may leave it in
# the first character.
# ---------------------------------------------------------------------
if (-not $Config) { $Config = Join-Path $PSScriptRoot 'pg-sql-alerts.json' }
if (-not (Test-Path $Config)) { Die "data file not found: $Config" }

try {
    $raw = [System.IO.File]::ReadAllText($Config, [System.Text.Encoding]::UTF8)
    $raw = $raw.TrimStart([char]0xFEFF)
    $cfg = $raw | ConvertFrom-Json
} catch {
    Die "cannot parse $Config : $($_.Exception.Message)"
}

if (-not $Folder)       { $Folder       = $cfg.folder }
if (-not $ContactPoint) { $ContactPoint = $cfg.contactPoint }

$headers = @{
    Authorization          = "Bearer $Token"
    'Content-Type'         = 'application/json; charset=utf-8'
    # Without this header Grafana marks the rules as externally provisioned
    # and refuses to let anyone edit them in the UI.
    'X-Disable-Provenance' = 'true'
}

# The request body is sent as BYTES, not as a string. Windows PowerShell 5.1
# encodes a string body with a non-UTF-8 default, which would silently turn
# the Russian rule titles into garbage inside Grafana.
function Invoke-Api {
    param($Method, $Uri, $Obj)
    $req = @{ Headers = $headers; Method = $Method; Uri = $Uri }
    if ($null -ne $Obj) {
        $json = $Obj | ConvertTo-Json -Depth 20 -Compress
        $req.Body = [System.Text.Encoding]::UTF8.GetBytes($json)
    }
    Invoke-RestMethod @req
}

function New-RuleBody {
    param($Title, $Group, $FolderUid, $DsUid, $Sql, $Summary, $Threshold, $HostName)
    @{
        title        = $Title
        ruleGroup    = $Group
        folderUID    = $FolderUid
        condition    = 'C'
        for          = '0s'        # a transaction age only grows, nothing to debounce
        noDataState  = 'OK'        # no hung transactions is silence, not breakage
        execErrState = 'Alerting'  # losing the database connection must be audible
        labels       = @{
            pg_group    = $cfg.pgGroup
            pg_instance = $HostName
            severity    = $cfg.severity
        }
        annotations           = @{ summary = $Summary }
        notification_settings = @{ receiver = $ContactPoint }
        data = @(
            @{
                refId             = 'A'
                relativeTimeRange = @{ from = 600; to = 0 }
                datasourceUid     = $DsUid
                model = @{
                    datasource    = @{ type = 'grafana-postgresql-datasource'; uid = $DsUid }
                    editorMode    = 'code'
                    format        = 'table'
                    instant       = $true
                    intervalMs    = 1000
                    maxDataPoints = 43200
                    rawQuery      = $true
                    rawSql        = $Sql
                    refId         = 'A'
                }
            },
            @{
                refId         = 'B'
                queryType     = 'expression'
                datasourceUid = '__expr__'
                model = @{
                    datasource    = @{ type = '__expr__'; uid = '__expr__' }
                    expression    = 'A'
                    intervalMs    = 1000
                    maxDataPoints = 43200
                    reducer       = 'last'
                    refId         = 'B'
                    type          = 'reduce'
                }
            },
            @{
                refId         = 'C'
                queryType     = 'expression'
                datasourceUid = '__expr__'
                model = @{
                    conditions = @(@{
                        evaluator = @{ params = @($Threshold); type = 'gt' }
                        operator  = @{ type = 'and' }
                        query     = @{ params = @('C') }
                        reducer   = @{ params = @(); type = 'last' }
                        type      = 'query'
                    })
                    datasource    = @{ type = '__expr__'; uid = '__expr__' }
                    expression    = 'B'
                    intervalMs    = 1000
                    maxDataPoints = 43200
                    refId         = 'C'
                    type          = 'threshold'
                }
            }
        )
    }
}

# ---------------------------------------------------------------------
Step "Grafana: $GrafanaUrl"
Step "data file: $Config"
Step ("hosts: " + $cfg.hosts.Count + ", checks: " + $cfg.checks.Count +
      ", rules to make: " + ($cfg.hosts.Count * $cfg.checks.Count))

try {
    $folders = Invoke-Api -Method Get -Uri "$GrafanaUrl/api/folders"
} catch {
    Die "API did not answer or token rejected: $($_.Exception.Message)"
}

$f = $folders | Where-Object { $_.title -eq $Folder } | Select-Object -First 1
if (-not $f) {
    if ($DryRun) {
        Step "folder '$Folder' would be created"
        $folderUid = '<new-folder>'
    } else {
        Step "creating folder '$Folder'"
        $f = Invoke-Api -Method Post -Uri "$GrafanaUrl/api/folders" -Obj @{ title = $Folder }
        $folderUid = $f.uid
    }
} else {
    $folderUid = $f.uid
    Step "folder '$Folder' found: $folderUid"
}

# What already exists, so a repeat run does not create duplicates.
$existing = @{}
try {
    foreach ($r in (Invoke-Api -Method Get -Uri "$GrafanaUrl/api/v1/provisioning/alert-rules")) {
        if ($r.folderUID -eq $folderUid) { $existing["$($r.ruleGroup)/$($r.title)"] = $true }
    }
} catch {
    Warn "could not list existing rules, duplicate check skipped"
}

$made = 0; $skipped = 0; $failed = 0

foreach ($h in $cfg.hosts) {
    $group = $cfg.groupPrefix + $h.name
    foreach ($c in $cfg.checks) {
        $thr = if ($null -ne $c.threshold) { $c.threshold } else { $h.longTxSec }
        $key = "$group/$($c.title)"

        if ($existing.ContainsKey($key)) {
            Warn "exists, skipping: $key"
            $skipped++
            continue
        }

        $body = New-RuleBody -Title $c.title -Group $group -FolderUid $folderUid `
                             -DsUid $h.ds -Sql $c.sql -Summary $c.summary `
                             -Threshold $thr -HostName $h.name

        if ($DryRun) {
            Step "[dry run] $($h.name) / $($c.title), threshold $thr"
            $made++
            continue
        }

        try {
            Invoke-Api -Method Post -Uri "$GrafanaUrl/api/v1/provisioning/alert-rules" -Obj $body | Out-Null
            Step "created: $($h.name) / $($c.title), threshold $thr"
            $made++
        } catch {
            $msg = $_.Exception.Message
            if ($_.ErrorDetails.Message) { $msg = $_.ErrorDetails.Message }
            Warn "failed: $key"
            Warn "   $msg"
            $failed++
        }
    }

    # Group evaluation interval. A separate call: creating a rule does not
    # set it, and the group would stay on the default.
    if (-not $DryRun) {
        try {
            Invoke-Api -Method Put `
                -Uri "$GrafanaUrl/api/v1/provisioning/folder/$folderUid/rule-groups/$group" `
                -Obj @{ title = $group; folderUid = $folderUid; interval = $cfg.evalIntervalSec } | Out-Null
        } catch {
            Warn "evaluation interval not set for group $group, check it in the UI"
        }
    }
}

Write-Host ""
Step "created $made, skipped $skipped, failed $failed"
if ($failed -gt 0) {
    Warn "If the error mentions a duplicate title, this Grafana requires titles to be"
    Warn "unique per folder rather than per group. Add the host name to 'title' in the JSON."
}
if (-not $DryRun -and $made -gt 0) {
    Write-Host ""
    Write-Host "Next: check in Alerting -> Alert rules that the rules are not paused," -ForegroundColor Cyan
    Write-Host "and delete the single hand-made rule in the BI folder." -ForegroundColor Cyan
}
