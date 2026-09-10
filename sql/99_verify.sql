-- =====================================================================
-- 99_verify.sql — прогнать ПОД РОЛЬЮ monitoring, чтобы убедиться,
-- что прав хватает и мониторинг увидит всё, что должен.
--
--   psql "postgresql://monitoring:ПАРОЛЬ@host:5432/db" -f sql/99_verify.sql
--
-- Все строки должны быть ok=t. Любое f — смотри подсказку в hint.
-- =====================================================================

\pset border 2

WITH checks AS (
  SELECT 'подключение и версия'            AS check_name,
         version()                          AS value,
         true                               AS ok,
         ''                                 AS hint
  UNION ALL
  SELECT 'роль pg_monitor выдана',
         current_user,
         pg_has_role(current_user, 'pg_monitor', 'member'),
         'выполни: GRANT pg_monitor TO monitoring;'
  UNION ALL
  SELECT 'видно чужие сессии (pg_read_all_stats)',
         count(*)::text || ' сессий',
         count(*) FILTER (WHERE query IS NOT NULL) > 0,
         'без pg_read_all_stats поля query/state будут NULL у чужих сессий'
  FROM pg_stat_activity
  UNION ALL
  SELECT 'читаются настройки (pg_read_all_settings)',
         COALESCE(current_setting('data_directory', true), '<скрыто>'),
         current_setting('data_directory', true) IS NOT NULL,
         'выполни: GRANT pg_read_all_settings TO monitoring;'
  UNION ALL
  SELECT 'схема monitoring доступна',
         COALESCE((SELECT nspname FROM pg_namespace WHERE nspname='monitoring'), '<нет>'),
         EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='monitoring'),
         'прогони sql/02_monitoring_schema.sql суперюзером'
  UNION ALL
  SELECT 'pg_stat_statements (опционально)',
         COALESCE(monitoring.pgss_schema(), '<не установлен>'),
         monitoring.pgss_schema() IS NOT NULL,
         'не критично: пустым будет только раздел "медленные запросы". См. sql/03_pg_stat_statements.sql'
)
SELECT
  CASE WHEN ok THEN 'OK' ELSE 'FAIL' END      AS "статус",
  check_name                                   AS "проверка",
  value                                    AS "значение",
  CASE WHEN ok THEN '' ELSE hint END       AS "что делать"
FROM checks;
