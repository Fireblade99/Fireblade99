<#
.SYNOPSIS
    Creates Grafana alert rules that query PostgreSQL directly.

.DESCRIPTION
    Two deliberate constraints keep this file runnable on Windows
    PowerShell 5.1, which is stricter than PowerShell 7:

    1. Pure ASCII. All Russian text, all SQL and all thresholds live in
       pg-sql-alerts.json next to this file, read with an explicitly
       specified UTF-8 encoding. A .ps1 without a BOM is decoded in the
       system codepage by 5.1, and a BOM is easily lost in transit, so
       the script simply contains nothing that could be mis-decoded.

    2. No multi-line hash literals, no here-strings, no backtick line
       continuations. Every structure is assembled key by key. Verbose,
       but it parses the same way on every version.

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
# would fall back to the system codepage, which is what we are avoiding.
# A stray BOM is trimmed by hand because .NET may leave it in place.
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

$headers = @{}
$headers['Authorization'] = "Bearer $Token"
$headers['Content-Type']  = 'application/json; charset=utf-8'
# Without this header Grafana marks the rules as externally provisioned
# and refuses to let anyone edit them in the UI.
$headers['X-Disable-Provenance'] = 'true'

# The request body is sent as BYTES, not as a string. Windows PowerShell
# 5.1 encodes a string body with a non-UTF-8 default, which would turn
# the Russian rule titles into garbage inside Grafana without any error.
function Invoke-Api {
    param($Method, $Uri, $Obj)
    $req = @{}
    $req['Headers'] = $headers
    $req['Method']  = $Method
    $req['Uri']     = $Uri
    if ($null -ne $Obj) {
        $json = $Obj | ConvertTo-Json -Depth 20 -Compress
        $req['Body'] = [System.Text.Encoding]::UTF8.GetBytes($json)
    }
    Invoke-RestMethod @req
}

function New-Expr {
    param($RefId, $Type, $Source, $Threshold)

    $ds = @{}
    $ds['type'] = '__expr__'
    $ds['uid']  = '__expr__'

    $model = @{}
    $model['datasource']    = $ds
    $model['expression']    = $Source
    $model['intervalMs']    = 1000
    $model['maxDataPoints'] = 43200
    $model['refId']         = $RefId
    $model['type']          = $Type

    if ($Type -eq 'reduce') {
        $model['reducer'] = 'last'
    } else {
        $ev = @{}
        $ev['params'] = @($Threshold)
        $ev['type']   = 'gt'

        $cond = @{}
        $cond['evaluator'] = $ev
        $cond['operator']  = @{ type = 'and' }
        $cond['query']     = @{ params = @($RefId) }
        $cond['reducer']   = @{ params = @(); type = 'last' }
        $cond['type']      = 'query'

        $model['conditions'] = @($cond)
    }

    $node = @{}
    $node['refId']         = $RefId
    $node['queryType']     = 'expression'
    $node['datasourceUid'] = '__expr__'
    $node['model']         = $model
    return $node
}

function New-SqlQuery {
    param($DsUid, $Sql)

    $ds = @{}
    $ds['type'] = 'grafana-postgresql-datasource'
    $ds['uid']  = $DsUid

    $model = @{}
    $model['datasource']    = $ds
    $model['editorMode']    = 'code'
    $model['format']        = 'table'
    $model['instant']       = $true
    $model['intervalMs']    = 1000
    $model['maxDataPoints'] = 43200
    $model['rawQuery']      = $true
    $model['rawSql']        = $Sql
    $model['refId']         = 'A'

    $range = @{}
    $range['from'] = 600
    $range['to']   = 0

    $node = @{}
    $node['refId']             = 'A'
    $node['relativeTimeRange'] = $range
    $node['datasourceUid']     = $DsUid
    $node['model']             = $model
    return $node
}

function New-RuleBody {
    param($Title, $Group, $FolderUid, $DsUid, $Sql, $Summary, $Threshold, $HostName)

    $labels = @{}
    $labels['pg_group']    = $cfg.pgGroup
    $labels['pg_instance'] = $HostName
    $labels['severity']    = $cfg.severity

    $body = @{}
    $body['title']        = $Title
    $body['ruleGroup']    = $Group
    $body['folderUID']    = $FolderUid
    $body['condition']    = 'C'
    $body['for']          = '0s'        # a transaction age only grows, nothing to debounce
    $body['noDataState']  = 'OK'        # no hung transactions is silence, not breakage
    $body['execErrState'] = 'Alerting'  # losing the database connection must be audible
    $body['labels']       = $labels
    $body['annotations']  = @{ summary = $Summary }
    $body['notification_settings'] = @{ receiver = $ContactPoint }

    $a = New-SqlQuery -DsUid $DsUid -Sql $Sql
    $b = New-Expr -RefId 'B' -Type 'reduce' -Source 'A'
    $c = New-Expr -RefId 'C' -Type 'threshold' -Source 'B' -Threshold $Threshold

    $body['data'] = @($a, $b, $c)
    return $body
}

# ---------------------------------------------------------------------
$total = $cfg.hosts.Count * $cfg.checks.Count
Step "Grafana: $GrafanaUrl"
Step "data file: $Config"
Step "hosts: $($cfg.hosts.Count), checks: $($cfg.checks.Count), rules to make: $total"

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
    $all = Invoke-Api -Method Get -Uri "$GrafanaUrl/api/v1/provisioning/alert-rules"
    foreach ($r in $all) {
        if ($r.folderUID -eq $folderUid) { $existing["$($r.ruleGroup)/$($r.title)"] = $true }
    }
} catch {
    Warn "could not list existing rules, duplicate check skipped"
}

$made = 0
$skipped = 0
$failed = 0

foreach ($h in $cfg.hosts) {
    $group = $cfg.groupPrefix + $h.name

    foreach ($c in $cfg.checks) {
        $thr = $c.threshold
        if ($null -eq $thr) { $thr = $h.longTxSec }
        $key = "$group/$($c.title)"

        if ($existing.ContainsKey($key)) {
            Warn "exists, skipping: $key"
            $skipped++
            continue
        }

        $body = New-RuleBody -Title $c.title -Group $group -FolderUid $folderUid -DsUid $h.ds -Sql $c.sql -Summary $c.summary -Threshold $thr -HostName $h.name

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
        $gi = @{}
        $gi['title']     = $group
        $gi['folderUid'] = $folderUid
        $gi['interval']  = $cfg.evalIntervalSec
        try {
            Invoke-Api -Method Put -Uri "$GrafanaUrl/api/v1/provisioning/folder/$folderUid/rule-groups/$group" -Obj $gi | Out-Null
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
