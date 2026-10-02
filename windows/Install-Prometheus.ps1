<#
.SYNOPSIS
    Разворачивает Prometheus на Windows-ноде для сбора метрик Postgres.

.DESCRIPTION
    Ставит только Prometheus. postgres_exporter и node_exporter уже
    работают на хосте БД — их трогать не нужно.

    Что делает:
      1. проверяет, что нода вообще достаёт до хоста БД по 9187 и 9100
      2. качает Prometheus и сверяет контрольную сумму
      3. пишет конфиг с двумя целями
      4. регистрирует задачу автозапуска
      5. открывает 9090 только для Grafana
      6. проверяет, что все цели поднялись

    Идемпотентен: повторный запуск обновляет конфиг и перезапускает задачу.

.EXAMPLE
    .\Install-Prometheus.ps1 -DbHost db01.corp.local -GrafanaAddress 10.0.5.20

.NOTES
    Запускать в PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА.
#>
[CmdletBinding()]
param(
    # Хост, на котором работают экспортёры (порты 9187 и 9100).
    [Parameter(Mandatory = $true)][string] $DbHost,

    # IP или подсеть Grafana. Без него 9090 пришлось бы открыть всей
    # сети, а там имена баз и вся конфигурация Postgres в открытом виде.
    [string] $GrafanaAddress,

    # Должно совпадать с меткой pg_instance в дашборде.
    [string] $InstanceName = 'prod-postgres',

    [string] $InstallDir    = 'C:\Prometheus',
    [int]    $RetentionDays = 90,
    [string] $Version       = '2.53.3',
    [int]    $Port          = 9090
)

$ErrorActionPreference = 'Stop'

# Сумма снята с prometheus-2.53.3.windows-amd64.zip. При смене версии
# в -Version эту строку тоже надо обновить, иначе проверка не пройдёт.
$ExpectedSha256 = '91081D06538800454C01CD21269D2AFA6F2A07C4A559D00162CB9A7CCE0F64B1'

function Step { param($m) Write-Host "==> $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "!!  $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "ОШИБКА: $m" -ForegroundColor Red; exit 1 }

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Die 'нужны права администратора' }

# ---------------------------------------------------------------------
# 1. Достаём ли до экспортёров ИМЕННО С ЭТОЙ ноды
#
# Проверка первым делом и намеренно: если сети нет, всё остальное —
# впустую. То, что curl работал с другой машины, ничего не доказывает.
# ---------------------------------------------------------------------
Step "проверяю доступность $DbHost"
$unreachable = @()
foreach ($p in 9187, 9100) {
    $ok = Test-NetConnection -ComputerName $DbHost -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue
    if ($ok) { Step "  $DbHost`:$p доступен" }
    else     { $unreachable += $p; Warn "  $DbHost`:$p НЕДОСТУПЕН" }
}
if ($unreachable.Count -gt 0) {
    Die ("нет доступа к портам: " + ($unreachable -join ', ') + ".`n" +
         "       Нужно правило на файрволе хоста БД, разрешающее вход с этой ноды.`n" +
         "       Без этого Prometheus поднимется, но цели будут DOWN, а дашборд пустой.")
}

# ---------------------------------------------------------------------
# 2. Скачать и проверить
# ---------------------------------------------------------------------
$exe = Join-Path $InstallDir 'prometheus.exe'
if (Test-Path $exe) {
    Step "Prometheus уже установлен в $InstallDir, скачивание пропускаю"
} else {
    $zip = Join-Path $env:TEMP "prometheus-$Version.zip"
    Step "качаю Prometheus $Version"
    Invoke-WebRequest -UseBasicParsing -OutFile $zip `
        -Uri "https://github.com/prometheus/prometheus/releases/download/v$Version/prometheus-$Version.windows-amd64.zip"

    $actual = (Get-FileHash $zip -Algorithm SHA256).Hash
    if ($actual -ne $ExpectedSha256) {
        Die ("контрольная сумма не совпала.`n" +
             "       ожидалась: $ExpectedSha256`n" +
             "       получена:  $actual`n" +
             "       Если менял -Version, обнови `$ExpectedSha256 в скрипте.")
    }
    Step 'sha256 совпал'

    $tmpDir = Join-Path $env:TEMP "prom-unpack-$Version"
    if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $tmpDir -Force
    $inner = Join-Path $tmpDir "prometheus-$Version.windows-amd64"

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item (Join-Path $inner '*') $InstallDir -Recurse -Force
    Remove-Item $tmpDir -Recurse -Force
    Remove-Item $zip -Force
    Step "установлен в $InstallDir"
}

New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'data') | Out-Null

# ---------------------------------------------------------------------
# 3. Конфиг
#
# Кодировка ascii намеренно: -Encoding UTF8 в Windows PowerShell 5.1
# дописывает BOM, и парсер YAML на нём спотыкается. Конфиг целиком
# в ASCII, так что ничего не теряется.
# ---------------------------------------------------------------------
Step 'пишу конфиг'
$configPath = Join-Path $InstallDir 'prometheus.yml'
@"
# Сгенерирован Install-Prometheus.ps1. Правки переживут повторный
# запуск скрипта только если сохранить их отдельно.

global:
  scrape_interval: 15s
  scrape_timeout: 10s        # обязан быть меньше scrape_interval

scrape_configs:

  # Метка pg_instance ОБЯЗАНА совпадать в обоих job: по ней дашборд
  # сшивает метрики базы (9187) и метрики хоста (9100). Автоматическая
  # метка instance для этого не годится — у них разные порты.

  - job_name: postgres-exporter
    static_configs:
      - targets: ["${DbHost}:9187"]
        labels:
          pg_instance: "$InstanceName"

  - job_name: node-exporter
    static_configs:
      - targets: ["${DbHost}:9100"]
        labels:
          pg_instance: "$InstanceName"

  # Сам Prometheus — чтобы заметить, если мониторинг сломался.
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:$Port"]
"@ | Set-Content -Path $configPath -Encoding ascii

$promtool = Join-Path $InstallDir 'promtool.exe'
& $promtool check config $configPath
if ($LASTEXITCODE -ne 0) { Die 'конфиг не прошёл проверку promtool' }

# ---------------------------------------------------------------------
# 4. Автозапуск
#
# Планировщик, а не служба Windows: prometheus.exe — обычное консольное
# приложение, диспетчер служб запустит его и решит, что оно «не
# отвечает», потому что оно не рапортует о старте. Планировщик держит
# консольные программы нормально и сам перезапускает после падения
# и после перезагрузки ноды.
# ---------------------------------------------------------------------
Step 'регистрирую задачу автозапуска'
$arguments = @(
    "--config.file=$configPath"
    "--storage.tsdb.path=$(Join-Path $InstallDir 'data')"
    "--storage.tsdb.retention.time=${RetentionDays}d"
    "--web.listen-address=0.0.0.0:$Port"
) -join ' '

$action    = New-ScheduledTaskAction -Execute $exe -Argument $arguments -WorkingDirectory $InstallDir
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName 'prometheus' -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Stop-ScheduledTask  -TaskName 'prometheus' -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName 'prometheus'

# ---------------------------------------------------------------------
# 5. Файрвол
# ---------------------------------------------------------------------
Get-NetFirewallRule -DisplayName 'Prometheus web' -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue

if ($GrafanaAddress) {
    Step "открываю $Port только для $GrafanaAddress"
    New-NetFirewallRule -DisplayName 'Prometheus web' -Direction Inbound `
        -Protocol TCP -LocalPort $Port -Action Allow -RemoteAddress $GrafanaAddress | Out-Null
} else {
    Warn "не задан -GrafanaAddress: правило файрвола НЕ создано."
    Warn "Grafana до порта $Port не достучится, пока не откроешь его вручную."
    Warn "Открывать всей сети не стоит: в метриках имена баз и вся конфигурация Postgres."
}

# ---------------------------------------------------------------------
# 6. Проверка
# ---------------------------------------------------------------------
Step 'жду первый сбор метрик'
Start-Sleep -Seconds 20

try {
    $health = Invoke-RestMethod "http://localhost:$Port/-/healthy" -TimeoutSec 10
    Step "Prometheus отвечает: $health"
} catch {
    Die "Prometheus не поднялся. Запусти вручную для диагностики:`n       $exe $arguments"
}

$targets = (Invoke-RestMethod "http://localhost:$Port/api/v1/targets").data.activeTargets
Write-Host ''
Write-Host 'Цели:'
$down = 0
foreach ($t in $targets) {
    $mark = if ($t.health -eq 'up') { 'OK  ' } else { $down++; 'DOWN' }
    Write-Host ("  {0} {1,-20} {2}" -f $mark, $t.labels.job, $t.lastError)
}

Write-Host ''
if ($down -gt 0) {
    Warn "$down цел(ей) не отвечают — причина в колонке справа."
} else {
    Write-Host 'Все цели собираются.' -ForegroundColor Green
}

Write-Host ''
Write-Host 'Дальше, в Grafana:'
Write-Host "  1. Connections -> Data sources -> Add -> Prometheus"
Write-Host "     URL: http://$($env:COMPUTERNAME):$Port"
Write-Host "     Save & test -> 'Successfully queried the Prometheus API'"
Write-Host "  2. Dashboards -> New -> Import -> postgres-dashboard.json"
Write-Host "     В списке 'База' должно появиться: $InstanceName"
Write-Host ''
Write-Host 'Управление задачей:'
Write-Host '  Get-ScheduledTask prometheus | Get-ScheduledTaskInfo'
Write-Host '  Stop-ScheduledTask prometheus / Start-ScheduledTask prometheus'
