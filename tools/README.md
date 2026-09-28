# tools/

Каталог для локальных бинарников, которые **не хранятся в репозитории**.

## pg17 — портируемый PostgreSQL 17

`scripts\start-stack.bat` ожидает портируемую сборку PostgreSQL здесь:

```
tools\pg17\pgsql\bin\pg_ctl.exe
tools\pg17\pgsql\bin\initdb.exe
tools\pg17\pgsql\bin\psql.exe
```

Каталог `tools\pg17\` в `.gitignore`: полная сборка с pgAdmin 4 занимает ~950 МБ
(в т.ч. `pgAdmin4.exe` ~234 МБ), GitHub такие файлы отклоняет (лимит 100 МБ на файл).

Как поставить:

1. Скачать «PostgreSQL 17 · Windows x86-64 · binaries (zip archive)» с
   https://www.enterprisedb.com/download-postgresql-binaries
2. Распаковать так, чтобы получился путь `tools\pg17\pgsql\bin\pg_ctl.exe`.
   Достаточно компонента `pgsql` — pgAdmin и Stack Builder не нужны.
3. Если PostgreSQL 17 уже установлен в системе, можно не копировать сборку,
   а положить бинарники на PATH и вручную выполнить то, что делает
   `start-stack.bat` (см. README проекта).

Альтернатива: указать в `.env` `PGHOST/PGPORT/PGUSER/PGPASSWORD` внешнего
сервера PostgreSQL 17 — тогда `tools\pg17` вообще не нужен, а БД создаётся
скриптом `db\init_db.py`.
