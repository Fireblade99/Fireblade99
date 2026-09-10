#!/usr/bin/env python3
"""
pg-probe — измеряет ВРЕМЯ ОТКЛИКА Postgres снаружи, как его видит клиент.

postgres_exporter отдаёт статистику изнутри базы (счётчики, размеры,
блокировки), но не отвечает на вопрос "сколько миллисекунд база отвечает
нам прямо сейчас". Этот пробер отвечает — и раскладывает задержку на
составляющие, чтобы было видно, ЧТО именно тормозит:

  tcp_connect     — только сеть до хоста (сокет открылся)
  connect         — сеть + TLS + аутентификация (полный psycopg2.connect)
  ping            — SELECT 1 по свежесозданному соединению
  session_ping    — SELECT 1 по УЖЕ открытому соединению

Смысл разложения: если session_ping в норме, а connect вырос — база
здорова, а проблема в установке соединения (кончились слоты, тормозит
TLS, тупит DNS). Если вырос session_ping — тормозит сам сервер.

Настройка через переменные окружения (см. .env.example).
Метрики отдаются на http://0.0.0.0:$PGPROBE_PORT/metrics
"""
import logging
import os
import signal
import socket
import sys
import threading
import time

try:
    import psycopg2
    from psycopg2.extensions import parse_dsn
    from prometheus_client import (
        CollectorRegistry, Counter, Gauge, Histogram, start_http_server,
    )
except ImportError as exc:                                  # pragma: no cover
    sys.exit(f"нужны зависимости: pip install -r requirements.txt ({exc})")


# --- конфиг -----------------------------------------------------------
def _escape(value):
    """Значение для libpq-строки key=value: кавычки и слэши экранируются."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_conninfo():
    """
    Строка подключения либо целиком из PGPROBE_DSN, либо собранная из
    стандартных PG*-переменных.

    Второй путь предпочтительнее: пароль в URL пришлось бы кодировать
    процентами, и любой '@', '/' или '#' в нём молча ломает подключение.
    В формате key=value такой проблемы нет.
    """
    dsn = os.environ.get("PGPROBE_DSN", "").strip()
    if dsn:
        return dsn

    parts = {
        "host": os.environ.get("PGHOST", ""),
        "port": os.environ.get("PGPORT", "5432"),
        "dbname": os.environ.get("PGDATABASE", "postgres"),
        "user": os.environ.get("PGUSER", ""),
        "password": os.environ.get("PGPASSWORD", ""),
        # Удалённая база через интернет без TLS — плохая идея,
        # поэтому по умолчанию require, а не prefer.
        "sslmode": os.environ.get("PGSSLMODE", "require"),
    }
    return " ".join(f"{k}={_escape(v)}" for k, v in parts.items() if v)


DSN = build_conninfo()
PORT = int(os.environ.get("PGPROBE_PORT", "9899"))
INTERVAL = float(os.environ.get("PGPROBE_INTERVAL_SECONDS", "15"))
TIMEOUT = int(float(os.environ.get("PGPROBE_TIMEOUT_SECONDS", "5")))
# Доп. метрики тяжелее замера отклика и меняются медленно, поэтому
# снимаются раз в N циклов, а не каждый. 4 x 15с = раз в минуту.
GAP_EVERY = int(os.environ.get("PGPROBE_GAPS_EVERY_N_CYCLES", "4"))

# Границы гистограмм подобраны под удалённую базу через интернет:
# от субмиллисекунды (локалка) до 10 секунд (всё плохо).
BUCKETS = (.001, .0025, .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10)

logging.basicConfig(
    level=os.environ.get("PGPROBE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pg-probe")

registry = CollectorRegistry()

up = Gauge("pgprobe_up",
           "1 — база ответила на последней проверке, 0 — нет",
           registry=registry)
last_success = Gauge("pgprobe_last_success_timestamp_seconds",
                     "Unixtime последней успешной проверки",
                     registry=registry)
probes_total = Counter("pgprobe_probes_total",
                       "Всего выполнено проверок",
                       registry=registry)
failures_total = Counter("pgprobe_failures_total",
                         "Неудачные проверки с разбивкой по причине",
                         ["reason"], registry=registry)
server_info = Gauge("pgprobe_server_version_num",
                    "server_version_num базы (напр. 160013)",
                    registry=registry)

# ---------------------------------------------------------------------
# Метрики, которых НЕТ в стоковом postgres_exporter.
#
# Экспортёр на хосте БД закрывает почти всё, но четыре вещи не отдаёт
# вовсе, и все четыре важны. Пробер и так подключается к базе для
# замера отклика, поэтому снимает их заодно — отдельный сервис не нужен.
# ---------------------------------------------------------------------
gaps_up = Gauge("pgprobe_gaps_up",
                "1 — доп. метрики собраны успешно (отдельно от pgprobe_up)",
                registry=registry)

# 1. Wraparound. На 2 млрд транзакций Postgres останавливает запись.
#    В стоковом экспортёре этого нет вообще.
xid_age = Gauge("pgprobe_xid_age",
                "age(datfrozenxid) по базе. Приближение к 2e9 = аварийная остановка",
                ["datname"], registry=registry)
xid_age_limit = Gauge("pgprobe_xid_age_limit",
                      "autovacuum_freeze_max_age — порог принудительного автовакуума",
                      registry=registry)

# 2. Заблокированные сессии. В стоке pg_locks_count разбит по mode,
#    но не по granted, поэтому «кто-то кого-то ждёт» оттуда не достать.
blocked_sessions = Gauge("pgprobe_blocked_sessions",
                         "Сессии, ждущие снятия блокировки прямо сейчас",
                         registry=registry)

# 3. Аптайм именно Postgres. В стоке есть только process_start_time_seconds
#    самого экспортёра — по нему рестарт базы не увидеть.
postmaster_uptime = Gauge("pgprobe_postmaster_uptime_seconds",
                          "Аптайм Postgres. Резкое падение = база рестартовала",
                          registry=registry)

# 4. Медленные запросы. Коллектор pg_stat_statements в экспортёре
#    по умолчанию выключен и на хосте не включён.
#    Значения кумулятивные с момента pg_stat_statements_reset().
_ST_LABELS = ["queryid", "usename", "query"]
stmt_calls = Gauge("pgprobe_statement_calls",
                   "Число вызовов запроса (кумулятивно)",
                   _ST_LABELS, registry=registry)
stmt_total_ms = Gauge("pgprobe_statement_total_time_ms",
                      "Суммарное время выполнения запроса, мс (кумулятивно)",
                      _ST_LABELS, registry=registry)
stmt_mean_ms = Gauge("pgprobe_statement_mean_time_ms",
                     "Среднее время выполнения запроса, мс",
                     _ST_LABELS, registry=registry)

HIST = {}
LAST = {}
for _stage, _help in (
    ("tcp_connect", "Открытие TCP-сокета до хоста БД (чистая сеть)"),
    ("connect",     "Полное подключение: сеть + TLS + аутентификация"),
    ("ping",        "SELECT 1 по свежему соединению"),
    ("session_ping","SELECT 1 по уже открытому соединению"),
):
    HIST[_stage] = Histogram(f"pgprobe_{_stage}_seconds", _help,
                             buckets=BUCKETS, registry=registry)
    LAST[_stage] = Gauge(f"pgprobe_{_stage}_last_seconds",
                         _help + " (значение последней проверки)",
                         registry=registry)


def observe(stage, seconds):
    HIST[stage].observe(seconds)
    LAST[stage].set(seconds)


def classify(exc):
    """Причина падения одним словом — идёт меткой, поэтому список закрытый."""
    text = str(exc).lower()
    if isinstance(exc, socket.timeout) or "timeout" in text or "timed out" in text:
        return "timeout"
    if isinstance(exc, socket.gaierror) or "could not translate host name" in text:
        return "dns"
    if "password" in text or "authentication" in text or "role" in text:
        return "auth"
    if "too many clients" in text or "connection limit" in text:
        return "connection_limit"
    if "refused" in text or "no route" in text or "unreachable" in text:
        return "network"
    if "starting up" in text or "shutting down" in text or "recovery" in text:
        return "not_ready"
    # Сервер оборвал уже установленное соединение: рестарт базы,
    # pg_terminate_backend, idle-таймаут на файрволе. Отличается от
    # "network" тем, что коннект был, а потом пропал.
    if ("closed the connection" in text or "connection already closed" in text
            or "eof detected" in text or "server closed" in text):
        return "connection_lost"
    if "ssl" in text or "certificate" in text:
        return "tls"
    return "other"


class Prober:
    def __init__(self, dsn):
        self.dsn = dsn
        params = parse_dsn(dsn)
        self.host = params.get("host", "localhost")
        self.port = int(params.get("port", 5432))
        # Долгоживущее соединение для session_ping. Именно оно отделяет
        # "тормозит сервер" от "тормозит установка соединения".
        self.session = None
        # Взводится, если обёртки monitoring.pgss_top() нет в базе.
        self.statements_disabled = False
        self.cycle = 0

    # -- шаг 1: чистая сеть -------------------------------------------
    def probe_tcp(self):
        started = time.perf_counter()
        sock = socket.create_connection((self.host, self.port), timeout=TIMEOUT)
        sock.close()
        observe("tcp_connect", time.perf_counter() - started)

    # -- шаг 2 и 3: подключение и запрос по свежему соединению ---------
    def probe_fresh_connection(self):
        started = time.perf_counter()
        conn = psycopg2.connect(self.dsn, connect_timeout=TIMEOUT)
        observe("connect", time.perf_counter() - started)
        try:
            conn.autocommit = True
            started = time.perf_counter()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            observe("ping", time.perf_counter() - started)
            server_info.set(conn.server_version)
        finally:
            # Пробер не имеет права оставлять за собой висящие сессии:
            # он мониторит насыщение по коннектам, а не создаёт его.
            conn.close()

    # -- шаг 4: запрос по уже открытому соединению --------------------
    def probe_session(self):
        if self.session is None or self.session.closed:
            self.session = psycopg2.connect(self.dsn, connect_timeout=TIMEOUT)
            self.session.autocommit = True
        started = time.perf_counter()
        try:
            with self.session.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except psycopg2.Error:
            # Соединение протухло (рестарт базы, обрыв сети) — роняем его,
            # следующий цикл поднимет заново.
            try:
                self.session.close()
            except Exception:                               # noqa: BLE001
                pass
            self.session = None
            raise
        observe("session_ping", time.perf_counter() - started)

    # -- доп. метрики, которых нет в стоковом экспортёре ---------------
    def collect_gaps(self):
        """
        Снимает четыре метрики, отсутствующие в postgres_exporter.

        Отдельным коротким соединением, а не через self.session: тяжёлый
        или зависший запрос иначе испортил бы замер session_ping, ради
        которого сессия и держится.

        Ошибки сюда не поднимаются: недоступность доп. метрик не должна
        влиять на pgprobe_up — хелсчек важнее. Для них своя метрика
        pgprobe_gaps_up.
        """
        conn = psycopg2.connect(self.dsn, connect_timeout=TIMEOUT)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                # Мониторинг не должен ждать локи на проде.
                cur.execute("SET lock_timeout = '5s'")
                cur.execute("SET statement_timeout = '15s'")

                # --- wraparound по всем базам ---
                # Шаблонные базы тоже стареют и тоже способны довести
                # кластер до аварийной остановки, поэтому не фильтруем.
                cur.execute("SELECT datname, age(datfrozenxid)::bigint FROM pg_database")
                rows = cur.fetchall()
                xid_age.clear()
                for datname, age in rows:
                    xid_age.labels(datname=datname).set(age)

                # --- три скаляра одним запросом ---
                cur.execute("""
                    SELECT
                      (SELECT setting::bigint FROM pg_settings
                        WHERE name = 'autovacuum_freeze_max_age'),
                      (SELECT count(*) FROM pg_locks WHERE NOT granted),
                      EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))
                """)
                limit, blocked, uptime = cur.fetchone()
                xid_age_limit.set(limit)
                blocked_sessions.set(blocked)
                postmaster_uptime.set(uptime)

                self._collect_statements(cur)
        finally:
            conn.close()

    def _collect_statements(self, cur):
        """
        Топ медленных запросов через monitoring.pgss_top().

        Обёртка отдаёт пустой набор, если pg_stat_statements не
        установлен. Если нет и самой обёртки (не прогонялся
        sql/02_monitoring_schema.sql) — предупреждаем один раз и
        больше не пытаемся, чтобы не сыпать ошибками каждую минуту.
        """
        if self.statements_disabled:
            return
        try:
            cur.execute(
                "SELECT queryid, usename, calls, total_time_ms, mean_time_ms,"
                " query_snippet FROM monitoring.pgss_top(25)")
            rows = cur.fetchall()
        except psycopg2.Error as exc:
            self.statements_disabled = True
            log.warning(
                "медленные запросы собирать не буду: %s. "
                "Нужен sql/02_monitoring_schema.sql (и pg_stat_statements в базе).",
                str(exc).strip().splitlines()[0])
            return

        # Состав топ-25 меняется между снятиями. Без clear() исчезнувшие
        # запросы навсегда застыли бы с последним значением.
        stmt_calls.clear()
        stmt_total_ms.clear()
        stmt_mean_ms.clear()
        for queryid, usename, calls, total_ms, mean_ms, snippet in rows:
            labels = dict(queryid=str(queryid), usename=usename or "",
                          query=snippet or "")
            stmt_calls.labels(**labels).set(calls)
            stmt_total_ms.labels(**labels).set(total_ms)
            stmt_mean_ms.labels(**labels).set(mean_ms)

    def run_once(self):
        probes_total.inc()
        try:
            self.probe_tcp()
            self.probe_fresh_connection()
            self.probe_session()
        except Exception as exc:                            # noqa: BLE001
            reason = classify(exc)
            failures_total.labels(reason=reason).inc()
            up.set(0)
            log.warning("проверка не прошла (%s): %s",
                        reason, str(exc).strip().splitlines()[0])
            return False
        up.set(1)
        last_success.set(time.time())

        # Доп. метрики — реже, чем замер отклика, и строго после него:
        # хелсчек не должен зависеть от их успеха.
        self.cycle += 1
        if GAP_EVERY > 0 and self.cycle % GAP_EVERY == 1:
            try:
                self.collect_gaps()
                gaps_up.set(1)
            except Exception as exc:                        # noqa: BLE001
                gaps_up.set(0)
                log.warning("доп. метрики снять не удалось: %s",
                            str(exc).strip().splitlines()[0])
        log.debug("ok  tcp=%.1fмс connect=%.1fмс ping=%.1fмс session=%.1fмс",
                  LAST["tcp_connect"]._value.get() * 1000,
                  LAST["connect"]._value.get() * 1000,
                  LAST["ping"]._value.get() * 1000,
                  LAST["session_ping"]._value.get() * 1000)
        return True

    def close(self):
        if self.session is not None and not self.session.closed:
            self.session.close()


def main():
    if not DSN:
        sys.exit("не задано подключение: укажи PGPROBE_DSN либо PGHOST/PGUSER/PGPASSWORD")

    prober = Prober(DSN)
    stop = threading.Event()

    def shutdown(signum, _frame):
        log.info("получен сигнал %s, останавливаюсь", signum)
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    start_http_server(PORT, registry=registry)
    log.info("метрики на :%d/metrics, интервал %.0f с, цель %s:%d",
             PORT, INTERVAL, prober.host, prober.port)

    # up=0 до первой успешной проверки, а не "метрики нет":
    # отсутствие серии и лежащая база — разные вещи для алертов.
    up.set(0)

    while not stop.is_set():
        cycle_started = time.perf_counter()
        prober.run_once()
        # Ждём с учётом времени самой проверки, чтобы интервал не полз.
        stop.wait(max(0.0, INTERVAL - (time.perf_counter() - cycle_started)))

    prober.close()
    log.info("остановлен")


if __name__ == "__main__":
    main()
