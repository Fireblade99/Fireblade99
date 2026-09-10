-- =====================================================================
-- 01_monitoring_role.sql
-- Создаёт read-only роль для мониторинга.
-- Запускать СУПЕРПОЛЬЗОВАТЕЛЕМ, один раз на кластер.
--
--   psql "$SUPERUSER_DSN" -v mon_password="'СЮДА_ПАРОЛЬ'" -f sql/01_monitoring_role.sql
--
-- Роль намеренно НЕ имеет прав на чтение пользовательских данных:
-- pg_monitor даёт доступ только к статистике и настройкам.
-- =====================================================================

\set ON_ERROR_STOP on

-- Пароль передаётся через -v mon_password. Без него скрипт не пойдёт дальше.
\if :{?mon_password}
\else
  \echo '!! Не задан пароль. Запускай так:'
  \echo '!!   psql ... -v mon_password="''ПАРОЛЬ''" -f sql/01_monitoring_role.sql'
  \quit
\endif

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monitoring') THEN
    CREATE ROLE monitoring LOGIN;
    RAISE NOTICE 'роль monitoring создана';
  ELSE
    RAISE NOTICE 'роль monitoring уже существует, меняю только пароль и права';
  END IF;
END
$$;

ALTER ROLE monitoring WITH LOGIN PASSWORD :mon_password;

-- Ограничиваем число коннектов: даже если экспортёр залипнет и начнёт
-- плодить сессии, он не съест весь max_connections у приложения.
ALTER ROLE monitoring CONNECTION LIMIT 10;

-- Мониторинг никогда не должен блокировать прод. Если статистика
-- почему-то ждёт лок дольше 5 секунд — пусть падает его запрос, а не база.
ALTER ROLE monitoring SET lock_timeout = '5s';
ALTER ROLE monitoring SET statement_timeout = '30s';
ALTER ROLE monitoring SET idle_in_transaction_session_timeout = '10s';

-- pg_monitor (PG 10+) = pg_read_all_settings + pg_read_all_stats + pg_stat_scan_tables.
-- Это ровно то, что нужно экспортёру, и ничего сверх.
GRANT pg_monitor TO monitoring;

-- Права на подключение ко всем текущим базам кластера.
DO $$
DECLARE
  db text;
BEGIN
  FOR db IN SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate
  LOOP
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO monitoring', db);
  END LOOP;
END
$$;

\echo '== роль monitoring готова =='
