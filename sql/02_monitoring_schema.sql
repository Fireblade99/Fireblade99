-- =====================================================================
-- 02_monitoring_schema.sql
-- Схема monitoring + SECURITY DEFINER обёртки.
-- Запускать СУПЕРПОЛЬЗОВАТЕЛЕМ на КАЖДОЙ базе, которую мониторим.
--
--   psql "$SUPERUSER_DSN" -f sql/02_monitoring_schema.sql
--
-- Нужна ТОЛЬКО для раздела «медленные запросы». Всё остальное на
-- дашборде работает и без неё.
--
-- Зачем обёртка вокруг pg_stat_statements:
--   1. Расширение может быть НЕ установлено (включается только с
--      рестартом Postgres). Обёртка тогда возвращает пустой набор, и
--      сбор метрик не падает целиком.
--   2. Имена колонок разъехались в PG13 (total_time -> total_exec_time).
--      Обёртка прячет это различие, поэтому один и тот же вызов
--      работает на PG 12-17.
--   3. SECURITY DEFINER даёт роли monitoring видеть тексты всех
--      запросов, не выдавая ей прав сверх необходимого.
-- =====================================================================

\set ON_ERROR_STOP on

CREATE SCHEMA IF NOT EXISTS monitoring;
GRANT USAGE ON SCHEMA monitoring TO monitoring;

-- ---------------------------------------------------------------------
-- Установлен ли pg_stat_statements и в какой схеме
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION monitoring.pgss_schema()
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
  SELECT n.nspname::text
  FROM pg_extension e
  JOIN pg_namespace n ON n.oid = e.extnamespace
  WHERE e.extname = 'pg_stat_statements';
$$;

-- ---------------------------------------------------------------------
-- Топ-N запросов по суммарному времени выполнения.
-- Пустой набор, если расширение не стоит.
--
-- ВАЖНО про кардинальность: queryid уходит в Prometheus меткой, поэтому
-- p_limit держим маленьким (по умолчанию 25). Не поднимай его до сотен —
-- каждый новый queryid это новая временная серия.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION monitoring.pgss_top(p_limit integer DEFAULT 25)
RETURNS TABLE (
  queryid          text,
  datname          text,
  usename          text,
  calls            bigint,
  total_time_ms    double precision,
  mean_time_ms     double precision,
  rows_returned    bigint,
  shared_blks_hit  bigint,
  shared_blks_read bigint,
  query_snippet    text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
  ext_schema text := monitoring.pgss_schema();
  time_col   text;
  mean_col   text;
BEGIN
  IF ext_schema IS NULL THEN
    RETURN;  -- расширения нет — молча отдаём пустоту
  END IF;

  -- PG13+ переименовал total_time -> total_exec_time
  IF current_setting('server_version_num')::int >= 130000 THEN
    time_col := 'total_exec_time';
    mean_col := 'mean_exec_time';
  ELSE
    time_col := 'total_time';
    mean_col := 'mean_time';
  END IF;

  RETURN QUERY EXECUTE format($q$
    SELECT
      s.queryid::text,
      d.datname::text,
      r.rolname::text,
      s.calls,
      s.%1$I::double precision,
      s.%2$I::double precision,
      s.rows,
      s.shared_blks_hit,
      s.shared_blks_read,
      -- нормализованный текст, обрезанный: он идёт меткой в Prometheus
      left(regexp_replace(s.query, '\s+', ' ', 'g'), 120)
    FROM %3$I.pg_stat_statements s
    JOIN pg_database d ON d.oid = s.dbid
    JOIN pg_roles    r ON r.oid = s.userid
    WHERE d.datname = current_database()
    ORDER BY s.%1$I DESC
    LIMIT %4$L
  $q$, time_col, mean_col, ext_schema, p_limit);
END
$$;

-- ---------------------------------------------------------------------
-- Права: только выполнение, только роли monitoring.
-- ---------------------------------------------------------------------
REVOKE ALL ON FUNCTION monitoring.pgss_schema()        FROM PUBLIC;
REVOKE ALL ON FUNCTION monitoring.pgss_top(integer)    FROM PUBLIC;

GRANT EXECUTE ON FUNCTION monitoring.pgss_schema()     TO monitoring;
GRANT EXECUTE ON FUNCTION monitoring.pgss_top(integer) TO monitoring;

\echo '== схема monitoring готова =='
