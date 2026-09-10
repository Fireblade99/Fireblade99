<#
.SYNOPSIS
    Создаёт пять алертов по Postgres в Grafana через API.

.DESCRIPTION
    Альтернатива ручному созданию в UI. Работает удалённо, доступ к
    файловой системе сервера Grafana не нужен.

    Что нужно подготовить:
      Grafana -> Administration -> Users and access -> Service accounts
      -> Add service account -> роль Editor -> Add service account token.
      Токен показывается один раз.

    Правила создаются с заголовком X-Disable-Provenance, поэтому
    остаются РЕДАКТИРУЕМЫМИ в интерфейсе. Без него Grafana пометит их
    как provisioned и запретит менять руками.

.EXAMPLE
    .\Import-GrafanaAlerts.ps1 -GrafanaUrl https://grafana.corp -Token glsa_xxx `
        -DatasourceName prometheus-qse-vip -DryRun

.EXAMPLE
    .\Import-GrafanaAlerts.ps1 -GrafanaUrl https://grafana.corp -Token glsa_xxx `
        -DatasourceName prometheus-qse-vip -FolderTitle "BI"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $GrafanaUrl,
    [Parameter(Mandatory = $true)][string] $Token,

    # Имя датасорса Prometheus, как оно указано в Grafana.
    [Parameter(Mandatory = $true)][string] $DatasourceName,

    [string] $FolderTitle = 'PostgreSQL',
    [string] $RuleGroup   = 'postgres',

    # Печатает payload и ничего не создаёт.
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$GrafanaUrl = $GrafanaUrl.TrimEnd('/')

function Step { param($m) Write-Host "==> $m" -ForegroundColor Green }
function Die  { param($m) Write-Host "ОШИБКА: $m" -ForegroundColor Red; exit 1 }

$headers = @{
    Authorization = "Bearer $Token"
    'Content-Type' = 'application/json'
    # Без этого правила станут read-only в интерфейсе.
    'X-Disable-Provenance' = 'true'
}

# ---------------------------------------------------------------------
# UID датасорса и папки
# ---------------------------------------------------------------------
Step "ищу датасорс '$DatasourceName'"
try {
    $ds = Invoke-RestMethod -Headers $headers -Method Get `
        -Uri "$GrafanaUrl/api/datasources/name/$([uri]::EscapeDataString($DatasourceName))"
} catch {
    Die ("датасорс '$DatasourceName' не найден. " +
         "Открой Connections -> Data sources и возьми имя оттуда как есть.")
}
$dsUid = $ds.uid
Step "  uid = $dsUid"

Step "ищу папку '$FolderTitle'"
$folders = Invoke-RestMethod -Headers $headers -Method Get -Uri "$GrafanaUrl/api/folders"
$folder  = $folders | Where-Object { $_.title -eq $FolderTitle } | Select-Object -First 1
if (-not $folder) {
    if ($DryRun) {
        Step "  папки нет, в обычном запуске была бы создана"
        $folderUid = '<новая-папка>'
    } else {
        Step "  создаю"
        $folder = Invoke-RestMethod -Headers $headers -Method Post `
            -Uri "$GrafanaUrl/api/folders" -Body (@{ title = $FolderTitle } | ConvertTo-Json)
        $folderUid = $folder.uid
    }
} else {
    $folderUid = $folder.uid
}
Step "  uid = $folderUid"

# ---------------------------------------------------------------------
# Определения правил
#
# Порог вынесен из PromQL в отдельный узел threshold намеренно: так его
# можно подвинуть мышкой в интерфейсе, не трогая запрос.
#
# noDataState:
#   OK       для правил 1-4. Если экспортёр умрёт, их метрики просто
#            исчезнут, и все четыре одновременно ушли бы в тревогу —
#            четыре уведомления об одной проблеме. Её ловит правило 5.
#   Alerting для правила 5. Метрика up пропадает, только если сам
#            Prometheus умер или потерял цель — а это надо знать.
# ---------------------------------------------------------------------
$rules = @(
    @{
        title = 'Postgres: база не отвечает'
        expr  = 'pg_up'
        op    = 'lt'; threshold = 1; for = '10s'; severity = 'critical'
        noData = 'OK'
        summary = 'Postgres не отвечает: {{ $labels.pg_instance }}'
        description = 'Экспортёр работает на самом хосте БД и ходит в Postgres через localhost. Раз он не может подключиться, проблема в базе, а не в сети до неё.'
    },
    @{
        title = 'Postgres: слоты подключений на исходе'
        expr  = 'sum by (pg_instance) (pg_stat_activity_count) / max by (pg_instance) (pg_settings_max_connections) * 100'
        op    = 'gt'; threshold = 90; for = '1m'; severity = 'critical'
        noData = 'OK'
        summary = 'Занято {{ $values.B }}% слотов на {{ $labels.pg_instance }}'
        description = 'Осталось меньше 10% от max_connections. Дальше новые клиенты получают отказ, а база остаётся живой — правило "база не отвечает" при этом молчит. Кто занял, видно на дашборде в панели "Кто занимает подключения".'
    },
    @{
        title = 'Postgres: диск базы кончается'
        expr  = '(1 - node_filesystem_avail_bytes{mountpoint=~"/data_db|/mwal"} / node_filesystem_size_bytes{mountpoint=~"/data_db|/mwal"}) * 100'
        op    = 'gt'; threshold = 85; for = '1m'; severity = 'critical'
        noData = 'OK'
        summary = '{{ $labels.mountpoint }} на {{ $labels.pg_instance }} занят на {{ $values.B }}%'
        description = 'Переполнение тома останавливает запись в Postgres мгновенно — это не деградация, а стоп. /data_db это данные, /mwal это WAL. Растущий /mwal при живой базе почти всегда означает застрявший слот репликации или сломанный archive_command.'
    },
    @{
        title = 'Postgres: транзакция висит больше часа'
        expr  = 'max by (pg_instance) (pg_stat_activity_max_tx_duration)'
        op    = 'gt'; threshold = 3600; for = '1m'; severity = 'warning'
        noData = 'OK'
        summary = 'Транзакция на {{ $labels.pg_instance }} открыта {{ $values.B }} секунд'
        description = 'Долгая транзакция удерживает старые версии строк и блокирует автовакуум по всей базе, а не только в своей таблице. Отсюда потом растут и распухание таблиц, и расход места на диске.'
    },
    @{
        title = 'Postgres: мониторинг ослеп'
        expr  = 'up{job=~"postgres-exporter|node-exporter"}'
        op    = 'lt'; threshold = 1; for = '1m'; severity = 'warning'
        noData = 'Alerting'
        summary = 'Не отвечает сборщик метрик {{ $labels.job }} ({{ $labels.instance }})'
        description = 'Prometheus не может опросить экспортёр. Пока это так, остальные алерты по этой базе слепы, а молчание мониторинга неотличимо от "всё хорошо".'
    }
)

function New-RulePayload {
    param($r, $dsUid, $folderUid, $group)
    @{
        title        = $r.title
        ruleGroup    = $group
        folderUID    = $folderUid
        condition    = 'C'
        noDataState  = $r.noData
        execErrState = 'Error'
        for          = $r.for
        orgID        = 1
        labels       = @{ severity = $r.severity }
        annotations  = @{ summary = $r.summary; description = $r.description }
        data = @(
            @{
                refId = 'A'
                relativeTimeRange = @{ from = 600; to = 0 }
                datasourceUid = $dsUid
                model = @{
                    refId = 'A'; expr = $r.expr
                    instant = $true; range = $false
                    editorMode = 'code'; legendFormat = '__auto'
                }
            },
            @{
                refId = 'B'
                relativeTimeRange = @{ from = 600; to = 0 }
                datasourceUid = '__expr__'
                model = @{
                    refId = 'B'; type = 'reduce'; expression = 'A'
                    reducer = 'last'; settings = @{ mode = 'dropNN' }
                }
            },
            @{
                refId = 'C'
                relativeTimeRange = @{ from = 600; to = 0 }
                datasourceUid = '__expr__'
                model = @{
                    refId = 'C'; type = 'threshold'; expression = 'B'
                    conditions = @(
                        @{
                            evaluator = @{ type = $r.op; params = @($r.threshold) }
                            operator  = @{ type = 'and' }
                            query     = @{ params = @('B') }
                            reducer   = @{ type = 'last'; params = @() }
                            type      = 'query'
                        }
                    )
                }
            }
        )
    }
}

# ---------------------------------------------------------------------
# Создание
# ---------------------------------------------------------------------
$created = 0; $failed = 0
foreach ($r in $rules) {
    $payload = New-RulePayload -r $r -dsUid $dsUid -folderUid $folderUid -group $RuleGroup
    $json = $payload | ConvertTo-Json -Depth 20

    if ($DryRun) {
        Write-Host ""
        Write-Host "--- $($r.title) ---" -ForegroundColor Cyan
        Write-Host $json
        continue
    }

    try {
        Invoke-RestMethod -Headers $headers -Method Post `
            -Uri "$GrafanaUrl/api/v1/provisioning/alert-rules" -Body $json | Out-Null
        Step "создано: $($r.title)"
        $created++
    } catch {
        $failed++
        $detail = $_.ErrorDetails.Message
        if (-not $detail) { $detail = $_.Exception.Message }
        Write-Host "!!  не удалось: $($r.title)" -ForegroundColor Yellow
        Write-Host "    $detail" -ForegroundColor Yellow
    }
}

if ($DryRun) {
    Write-Host ""
    Write-Host "Это был -DryRun, ничего не создано." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "Создано: $created, ошибок: $failed"
if ($failed -eq 0) {
    Write-Host "Проверить: Alerting -> Alert rules, папка '$FolderTitle'." -ForegroundColor Green
    Write-Host "Уведомления настраиваются отдельно: Alerting -> Notification policies."
}
