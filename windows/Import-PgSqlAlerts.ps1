<#
.SYNOPSIS
    Создаёт в Grafana правила алертинга, которые ходят напрямую в Postgres.

.DESCRIPTION
    Двенадцать правил: три проверки на четыре хоста группы prod-dp.
    Проверки те, где метрик недостаточно и нужен текст запроса:
    зависшая idle in transaction, долгая активная транзакция, блокировки.

    Одно правило = один датасорс, переменных в алертах нет — отсюда и
    умножение на хосты.

    Правила создаются через API провиженинга с заголовком
    X-Disable-Provenance: без него Grafana пометит их как управляемые
    извне и запретит правку мышкой.

.EXAMPLE
    .\Import-PgSqlAlerts.ps1 -GrafanaUrl http://grafana:3000 -Token glsa_xxx -DryRun
    .\Import-PgSqlAlerts.ps1 -GrafanaUrl http://grafana:3000 -Token glsa_xxx
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $GrafanaUrl,
    [Parameter(Mandatory)] [string] $Token,
    [string] $Folder       = 'PostgreSQL prod-dp',
    [string] $ContactPoint = 'telegram-db-dponline',
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$GrafanaUrl = $GrafanaUrl.TrimEnd('/')

function Step { param($m) Write-Host "==> $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "    $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "ОШИБКА: $m" -ForegroundColor Red; exit 1 }

$headers = @{
    Authorization          = "Bearer $Token"
    'Content-Type'         = 'application/json'
    # Без этого правила станут «только для чтения» и в интерфейсе их
    # нельзя будет поправить — ни порог, ни контакт-поинт.
    'X-Disable-Provenance' = 'true'
}

# ---------------------------------------------------------------------
# Хосты и их датасорсы.
# UID берётся в Connections -> Data sources -> открыть -> из адреса
# после /edit/. Перепутать местами легко, а заметить потом трудно:
# правило будет исправно опрашивать чужой хост.
# ---------------------------------------------------------------------
$hosts = @(
    @{ Name = 'dwh-dbp2-lp2';  Ds = 'fg005dyg7prswd'; LongTxSec = 60   },
    @{ Name = 'dwh-dbp3-lp1';  Ds = 'eg005bspuguf4a'; LongTxSec = 3600 },
    @{ Name = 'dwh-dbp10-lp2'; Ds = 'bg0059faugqgwd'; LongTxSec = 3600 },
    @{ Name = 'dwh-dbp12-lp2'; Ds = 'cg00541dw4jk0e'; LongTxSec = 3600 }
)

# ---------------------------------------------------------------------
# Запросы.
#
# Колонки текстовые становятся метками алерта, числовая — значением.
# Колонки со временем быть не должно: с ней Grafana считает кадр
# временным рядом и падает с «input data must be a wide series but got
# type long», по тексту которого догадаться невозможно.
#
# Порог в SQL не зашит, только нижний отсечной фильтр: сам порог живёт
# в узле Threshold, где его видно и правится мышкой.
# ---------------------------------------------------------------------
$sqlIdle = @'
SELECT
  pid::text                                           AS pid,
  usename                                             AS usename,
  coalesce(nullif(application_name,''),'-')           AS app,
  datname                                             AS datname,
  left(regexp_replace(query, E'\\s+', ' ', 'g'), 200) AS query,
  EXTRACT(EPOCH FROM (now() - state_change))::float8  AS seconds
FROM pg_stat_activity
WHERE backend_type = 'client backend'
  AND state LIKE 'idle in transaction%'
  AND now() - state_change > interval '30 seconds'
'@

$sqlLongTx = @'
SELECT
  pid::text                                           AS pid,
  usename                                             AS usename,
  coalesce(nullif(application_name,''),'-')           AS app,
  datname                                             AS datname,
  coalesce(wait_event_type,'-')                       AS wait_type,
  left(regexp_replace(query, E'\\s+', ' ', 'g'), 200) AS query,
  EXTRACT(EPOCH FROM (now() - xact_start))::float8    AS seconds
FROM pg_stat_activity
WHERE backend_type = 'client backend'
  AND state <> 'idle'
  AND xact_start IS NOT NULL
  AND now() - xact_start > interval '30 seconds'
'@

# pg_blocking_pids дорогая, поэтому зовётся только для сессий, которые
# реально стоят в очереди за блокировкой, а не для всех подряд.
$sqlBlocks = @'
WITH waiters AS (
  SELECT pid, usename, datname, query_start
  FROM pg_stat_activity
  WHERE wait_event_type = 'Lock'
    AND now() - query_start > interval '30 seconds'
)
SELECT
  w.pid::text                                           AS blocked_pid,
  coalesce(w.usename,'-')                               AS blocked_user,
  w.datname                                             AS datname,
  b.pid::text                                           AS blocking_pid,
  coalesce(b.usename,'-')                               AS blocking_user,
  coalesce(nullif(b.application_name,''),'-')           AS blocking_app,
  b.state                                               AS blocking_state,
  left(regexp_replace(b.query, E'\\s+', ' ', 'g'), 200) AS blocking_query,
  EXTRACT(EPOCH FROM (now() - w.query_start))::float8   AS seconds
FROM waiters w
CROSS JOIN LATERAL unnest(pg_blocking_pids(w.pid)) AS p(pid)
JOIN pg_stat_activity b ON b.pid = p.pid
'@

$summaryTx = @'
pid {{ $labels.pid }} · {{ $labels.usename }} / {{ $labels.app }} · база {{ $labels.datname }}
{{ $labels.query }}
'@

$summaryBlocks = @'
pid {{ $labels.blocked_pid }} ждёт · база {{ $labels.datname }}
держит pid {{ $labels.blocking_pid }} · {{ $labels.blocking_user }} / {{ $labels.blocking_app }} · {{ $labels.blocking_state }}
{{ $labels.blocking_query }}
'@

$checks = @(
    @{ Title = 'ЗависшаяIdleInTransaction'; Sql = $sqlIdle;   Summary = $summaryTx;     Threshold = 120 },
    @{ Title = 'ДолгаяТранзакция';          Sql = $sqlLongTx; Summary = $summaryTx;     Threshold = $null },  # из хоста
    @{ Title = 'СессииЖдутБлокировку';      Sql = $sqlBlocks; Summary = $summaryBlocks; Threshold = 120 }
)

function New-RuleBody {
    param($Title, $Group, $FolderUid, $DsUid, $Sql, $Summary, $Threshold, $HostName)
    @{
        title        = $Title
        ruleGroup    = $Group
        folderUID    = $FolderUid
        condition    = 'C'
        for          = '0s'      # возраст транзакции только растёт, дёргаться нечему
        noDataState  = 'OK'      # нет зависших — это тишина, а не поломка
        execErrState = 'Alerting'  # потеря связи с базой должна быть слышна
        labels       = @{
            pg_group    = 'prod-dp'
            pg_instance = $HostName
            severity    = 'warning'
        }
        annotations  = @{ summary = $Summary }
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

try {
    $folders = Invoke-RestMethod -Headers $headers -Method Get -Uri "$GrafanaUrl/api/folders"
} catch {
    Die "не отвечает API или не принят токен: $($_.Exception.Message)"
}

$f = $folders | Where-Object { $_.title -eq $Folder } | Select-Object -First 1
if (-not $f) {
    if ($DryRun) {
        Step "папка '$Folder' будет создана"
        $folderUid = '<новая-папка>'
    } else {
        Step "создаю папку '$Folder'"
        $f = Invoke-RestMethod -Headers $headers -Method Post -Uri "$GrafanaUrl/api/folders" `
                               -Body (@{ title = $Folder } | ConvertTo-Json)
        $folderUid = $f.uid
    }
} else {
    $folderUid = $f.uid
    Step "папка '$Folder' найдена: $folderUid"
}

# Что уже есть — чтобы повторный запуск не наплодил дублей.
$existing = @{}
try {
    foreach ($r in (Invoke-RestMethod -Headers $headers -Method Get -Uri "$GrafanaUrl/api/v1/provisioning/alert-rules")) {
        if ($r.folderUID -eq $folderUid) { $existing["$($r.ruleGroup)/$($r.title)"] = $true }
    }
} catch {
    Warn "не удалось получить список существующих правил, проверка на дубли пропущена"
}

$made = 0; $skipped = 0; $failed = 0

foreach ($h in $hosts) {
    $group = "pg-prod-dp-$($h.Name)"
    foreach ($c in $checks) {
        $thr = if ($null -ne $c.Threshold) { $c.Threshold } else { $h.LongTxSec }
        $key = "$group/$($c.Title)"

        if ($existing.ContainsKey($key)) {
            Warn "уже есть, пропускаю: $key"
            $skipped++
            continue
        }

        $body = New-RuleBody -Title $c.Title -Group $group -FolderUid $folderUid `
                             -DsUid $h.Ds -Sql $c.Sql -Summary $c.Summary `
                             -Threshold $thr -HostName $h.Name

        if ($DryRun) {
            Step "[проба] $($h.Name) / $($c.Title), порог $thr"
            $made++
            continue
        }

        try {
            Invoke-RestMethod -Headers $headers -Method Post `
                -Uri "$GrafanaUrl/api/v1/provisioning/alert-rules" `
                -Body ($body | ConvertTo-Json -Depth 20 -Compress) | Out-Null
            Step "создано: $($h.Name) / $($c.Title), порог $thr"
            $made++
        } catch {
            $msg = $_.Exception.Message
            if ($_.ErrorDetails.Message) { $msg = $_.ErrorDetails.Message }
            Warn "не создано: $key"
            Warn "   $msg"
            $failed++
        }
    }

    # Интервал вычисления группы. Отдельным вызовом: при создании
    # правила он не задаётся, и группа осталась бы на умолчании.
    if (-not $DryRun) {
        try {
            Invoke-RestMethod -Headers $headers -Method Put `
                -Uri "$GrafanaUrl/api/v1/provisioning/folder/$folderUid/rule-groups/$group" `
                -Body (@{ title = $group; folderUid = $folderUid; interval = 60 } | ConvertTo-Json) | Out-Null
        } catch {
            Warn "интервал группы $group не выставлен, проверь его в интерфейсе"
        }
    }
}

Write-Host ""
Step "создано $made, пропущено $skipped, с ошибкой $failed"
if ($failed -gt 0) {
    Warn "Если ошибка про неуникальное имя — значит, эта Grafana требует уникальности"
    Warn "в пределах папки, а не группы. Тогда допиши имя хоста в Title внутри `$checks."
}
if (-not $DryRun -and $made -gt 0) {
    Write-Host ""
    Write-Host "Дальше: проверить в Alerting -> Alert rules, что правила не на паузе," -ForegroundColor Cyan
    Write-Host "и удалить одиночное правило, собранное руками в папке BI." -ForegroundColor Cyan
}
