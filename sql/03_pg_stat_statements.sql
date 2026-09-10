-- =====================================================================
-- 03_pg_stat_statements.sql  (ОПЦИОНАЛЬНО, требует РЕСТАРТА Postgres)
--
-- pg_stat_statements — единственный способ увидеть, КАКИЕ запросы
-- тормозят, а не только что "база медленная". Без него мониторинг
-- работает, но раздел "медленные запросы" будет пустым.
--
-- Загвоздка: расширение живёт в shared_preload_libraries, а этот
-- параметр применяется только при старте процесса. ALTER SYSTEM ниже
-- отработает по SQL (суперюзер это может и без доступа к хосту),
-- но ПЕРЕЗАПУСК придётся сделать отдельно:
--   * managed БД (RDS / Cloud SQL / Yandex / Timeweb) — кнопка Restart
--     в консоли провайдера, либо параметр-группа;
--   * self-hosted — pg_ctl restart / systemctl restart postgresql
--     руками у того, у кого есть доступ к машине.
--
-- Если рестарт невозможен — просто не запускай этот файл. Всё остальное
-- продолжит работать, monitoring.pgss_top() вернёт пустоту.
-- =====================================================================

\set ON_ERROR_STOP on

-- Проверяем, не включено ли уже
SELECT
  current_setting('shared_preload_libraries') AS current_libs,
  current_setting('shared_preload_libraries') LIKE '%pg_stat_statements%' AS already_loaded
\gset

\if :already_loaded
  \echo '== pg_stat_statements уже в shared_preload_libraries, рестарт не нужен =='
\else
  \echo '!! pg_stat_statements НЕ загружен. Добавляю через ALTER SYSTEM.'
  \echo '!! ПОСЛЕ ЭТОГО НУЖЕН РЕСТАРТ POSTGRES, иначе изменения не вступят в силу.'
  -- Дописываем к тому, что уже есть, а не затираем: там могут быть
  -- pg_cron, auto_explain, pgaudit и прочее, что кто-то ставил до нас.
  SELECT format(
    'ALTER SYSTEM SET shared_preload_libraries = %L',
    CASE
      WHEN current_setting('shared_preload_libraries') = ''
        THEN 'pg_stat_statements'
      ELSE current_setting('shared_preload_libraries') || ',pg_stat_statements'
    END
  ) AS alter_stmt
  \gset
  :alter_stmt;
\endif

-- Эти три применяются без рестарта (нужен только reload), но имеют
-- смысл лишь когда библиотека уже загружена.
ALTER SYSTEM SET pg_stat_statements.track = 'top';
ALTER SYSTEM SET pg_stat_statements.max = 5000;
ALTER SYSTEM SET pg_stat_statements.save = on;

SELECT pg_reload_conf();

-- CREATE EXTENSION сработает только если библиотека реально загружена,
-- то есть при первом прогоне (до рестарта) он упадёт — это ожидаемо.
-- Поэтому оборачиваем, чтобы скрипт не прерывался.
DO $$
BEGIN
  CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
  RAISE NOTICE 'pg_stat_statements установлен и готов';
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'CREATE EXTENSION пока не проходит (%). Это нормально до рестарта — повтори этот файл после перезапуска Postgres.', SQLERRM;
END
$$;
