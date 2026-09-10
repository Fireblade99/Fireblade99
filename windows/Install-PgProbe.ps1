<#
.SYNOPSIS
    Ставит pg-probe (и по желанию Prometheus) как службы Windows.

.DESCRIPTION
    postgres_exporter и node_exporter уже работают на хосте БД — их
    ставить не нужно. Здесь разворачивается только то, что должно жить
    на НАШЕЙ стороне:

      pg-probe    измеряет время отклика базы по сети и снимает четыре
                  метрики, которых стоковый экспортёр не отдаёт
      Prometheus  опционально, если своего ещё нет

    Пробер намеренно стоит не на хосте БД: он меряет путь до базы с той
    стороны, с которой к ней ходит приложение. Экспортёр на самом хосте
    этого увидеть не может по определению.

.EXAMPLE
    .\Install-PgProbe.ps1 -PgHost db.example.com -PgPassword 'пароль' `
        -DbHostAddress db.example.com -WithPrometheus

.NOTES
    Запускать в PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $PgHost,
    [int]    $PgPort     = 5432,
    [string] $PgDatabase = 'postgres',
    [string] $PgUser     = 'monitoring',
    [Parameter(Mandatory = $true)][string] $PgPassword,

    # Удалённая база через сеть без TLS — плохая идея, поэтому require
    # по умолчанию. disable оправдан только в доверенной приватной сети.
    [ValidateSet('require', 'verify-full', 'prefer', 'disable')]
    [string] $SslMode = 'require',

    # Должно совпадать с меткой pg_instance в prometheus.yml — по ней
    # связываются метрики пробера и экспортёров.
    [string] $InstanceName = 'prod-postgres',

    [string] $InstallDir = 'C:\PgMonitoring',
    [int]    $ProbePort  = 9899,

    [switch] $WithPrometheus,
    # Адрес хоста БД для scrape-конфига Prometheus (порты 9187 и 9100).
    [string] $DbHostAddress,
    [string] $PrometheusVersion = '2.53.3'
)

$ErrorActionPreference = 'Stop'

function Write-Step { param($m) Write-Host "==> $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "!!  $m" -ForegroundColor Yellow }
function Die       { param($m) Write-Host "ОШИБКА: $m" -ForegroundColor Red; exit 1 }

# --- проверки ---------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Die 'нужны права администратора' }

$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python3.exe -ErrorAction SilentlyContinue).Source }
if (-not $python) {
    Die 'не найден Python. Поставь с python.org (галочка "Add to PATH") и запусти снова.'
}
Write-Step "Python: $python"

$repoRoot = Split-Path -Parent $PSScriptRoot

# --- каталоги ---------------------------------------------------------
Write-Step "каталог установки: $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item (Join-Path $repoRoot 'probe\probe.py')         $InstallDir -Force
Copy-Item (Join-Path $repoRoot 'probe\requirements.txt') $InstallDir -Force

# --- виртуальное окружение -------------------------------------------
# Отдельный venv, чтобы не конфликтовать с системными пакетами.
$venv = Join-Path $InstallDir 'venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    Write-Step 'создаю виртуальное окружение'
    & $python -m venv $venv
}
$venvPython = Join-Path $venv 'Scripts\python.exe'
Write-Step 'ставлю зависимости'
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $InstallDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Die 'не удалось поставить зависимости' }

# --- запускающий .cmd с паролем --------------------------------------
# Пароль лежит в файле, а не в аргументах команды: аргументы видны в
# списке процессов любому пользователю системы.
$runner = Join-Path $InstallDir 'run-probe.cmd'
@"
@echo off
set PGHOST=$PgHost
set PGPORT=$PgPort
set PGDATABASE=$PgDatabase
set PGUSER=$PgUser
set PGPASSWORD=$PgPassword
set PGSSLMODE=$SslMode
set PGPROBE_PORT=$ProbePort
set PGPROBE_INTERVAL_SECONDS=15
"$venvPython" -u "$InstallDir\probe.py"
"@ | Set-Content -Path $runner -Encoding ASCII

# Права: только SYSTEM и администраторы. Наследование убираем, иначе
# пароль прочитает любой пользователь машины.
Write-Step 'ограничиваю доступ к файлу с паролем'
icacls $runner /inheritance:r /grant:r 'SYSTEM:(F)' 'BUILTIN\Администраторы:(F)' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    # На англоязычной Windows группа называется иначе.
    icacls $runner /inheritance:r /grant:r 'SYSTEM:(F)' 'BUILTIN\Administrators:(F)' | Out-Null
}

# --- задача планировщика ---------------------------------------------
# Планировщик вместо службы: обычной программе на Python не нужен
# service-контроллер, а перезапуск при сбое планировщик умеет сам.
Write-Step 'регистрирую задачу pg-probe'
$action    = New-ScheduledTaskAction -Execute $runner
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName 'pg-probe' -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'pg-probe'

Start-Sleep -Seconds 6
try {
    $metrics = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 `
        -Uri "http://127.0.0.1:$ProbePort/metrics"
    if ($metrics.Content -match 'pgprobe_up 1') {
        Write-Step "пробер работает, база отвечает (http://127.0.0.1:$ProbePort/metrics)"
    } else {
        Write-Warn "пробер поднялся, но база НЕ отвечает. Смотри pgprobe_failures_total на /metrics"
    }
} catch {
    Die "пробер не отвечает на порту $ProbePort. Запусти вручную для диагностики: $runner"
}

# --- Prometheus (опционально) ----------------------------------------
if ($WithPrometheus) {
    if (-not $DbHostAddress) { Die 'для -WithPrometheus нужен -DbHostAddress' }

    $promDir = Join-Path $InstallDir "prometheus-$PrometheusVersion"
    if (-not (Test-Path (Join-Path $promDir 'prometheus.exe'))) {
        $zip = Join-Path $env:TEMP "prometheus-$PrometheusVersion.zip"
        $url = "https://github.com/prometheus/prometheus/releases/download/v$PrometheusVersion/prometheus-$PrometheusVersion.windows-amd64.zip"
        Write-Step "качаю Prometheus $PrometheusVersion"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $InstallDir -Force
        Rename-Item (Join-Path $InstallDir "prometheus-$PrometheusVersion.windows-amd64") $promDir -Force
        Remove-Item $zip -Force
    }

    Write-Step 'пишу конфиг Prometheus'
    New-Item -ItemType Directory -Force -Path (Join-Path $promDir 'rules') | Out-Null
    Copy-Item (Join-Path $repoRoot 'prometheus\rules\*.yml') (Join-Path $promDir 'rules') -Force

    (Get-Content (Join-Path $repoRoot 'prometheus\prometheus.yml') -Raw -Encoding UTF8) `
        -replace 'ЗАМЕНИТЬ-НА-АДРЕС-ХОСТА-БД:9187', "${DbHostAddress}:9187" `
        -replace 'ЗАМЕНИТЬ-НА-АДРЕС-ХОСТА-БД:9100', "${DbHostAddress}:9100" `
        -replace '127\.0\.0\.1:9899', "127.0.0.1:$ProbePort" `
        -replace 'pg_instance: "prod-postgres"', "pg_instance: `"$InstanceName`"" |
        Set-Content -Path (Join-Path $promDir 'prometheus.yml') -Encoding UTF8

    $promRunner = Join-Path $InstallDir 'run-prometheus.cmd'
    @"
@echo off
cd /d "$promDir"
prometheus.exe --config.file=prometheus.yml --storage.tsdb.path=data --storage.tsdb.retention.time=90d --web.listen-address=0.0.0.0:9090
"@ | Set-Content -Path $promRunner -Encoding ASCII

    Write-Step 'регистрирую задачу prometheus'
    $pAction = New-ScheduledTaskAction -Execute $promRunner
    Register-ScheduledTask -TaskName 'prometheus' -Action $pAction -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName 'prometheus'
    Start-Sleep -Seconds 8
    Write-Step 'Prometheus поднят на http://127.0.0.1:9090'
}

Write-Host ''
Write-Host 'Готово.' -ForegroundColor Green
Write-Host ''
Write-Host 'Дальше:'
Write-Host "  1. Проверить совместимость метрик с версией экспортёров у админов:"
Write-Host "       $venvPython $repoRoot\tools\check_targets.py ``"
Write-Host "           --postgres-exporter http://${PgHost}:9187/metrics ``"
Write-Host "           --node-exporter     http://${PgHost}:9100/metrics ``"
Write-Host "           --probe             http://127.0.0.1:$ProbePort/metrics"
Write-Host "  2. В Grafana добавить источник данных Prometheus (http://<эта машина>:9090)"
Write-Host "  3. Импортировать grafana\postgres-dashboard.json"
Write-Host ''
Write-Host 'Управление: Get-ScheduledTask pg-probe | Get-ScheduledTaskInfo'
