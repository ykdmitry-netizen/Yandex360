# Y360 Admin · Консоль администрирования Яндекс 360

Единая консоль жизненного цикла пользователей и почтовых ящиков:
синхронизация Active Directory → Яндекс 360, отчёт о расхождениях, архивация
(бэкап) ящиков уволенных, восстановление почты и сроки хранения по должностям.

Интерфейс — современная тёмная веб-консоль на [NiceGUI](https://nicegui.io)
(http://127.0.0.1:8080), данные — PostgreSQL. Для локальной отладки без боевых
доступов предусмотрены моки AD, IMAP и API Яндекс 360 — переключение режима
происходит через `.env`, без изменения кода.

## Возможности

| Раздел | Что внутри |
| :-- | :-- |
| **Дашборд** | Ключевые метрики, диаграмма статусов ящиков, последние снимки, быстрые действия |
| **Мастер синхронизации** | Сбор AD + Яндекс 360, план отделов, синхронизация карточек (DRY-RUN и применение), публикация «только в Яндексе» |
| **Расхождения** | Сравнение AD ↔ Яндекс 360 (логин / СНИЛС / employeeId / email), выгрузка отчёта XLSX |
| **Сотрудники** | Справочник с поиском, ручной бэкап ящика, история запусков по сотруднику |
| **Архивация** | Очередь уволенных, снимки ящиков, импорт событий, очистка устаревших снимков |
| **Восстановление** | Просмотр снимка (папки, письма), выгрузка EML/MBOX, восстановление обратно в Яндекс 360 |
| **Сроки хранения** | Правила по должностям: топ-менеджмент 3 мес, руководители 2 мес, закупки/склад 3 мес, ИТ 2 мес, остальные 14 дней |
| **Журнал** | Аудит операций, запуски бэкапов, сообщения приложения |
| **Настройки** | Проверка подключений (PostgreSQL, AD, API), режимы мок/боевое, параметры хранения |

## Быстрый старт (локальный стенд, Windows)

```powershell
cd y360-admin
python -m venv venv
venv\Scripts\pip install -r requirements.txt
scripts\start-stack.bat
```

`start-stack.bat` поднимает весь стек: PostgreSQL из `tools\pg17`, мок API
Яндекс 360 (порт 8600), применяет схему БД (идемпотентно) и запускает консоль.
Консоль: http://127.0.0.1:8080 · Остановка: `scripts\stop-stack.bat`.

`.env` с настройками демо-стенда (мок-токены, локальный пароль БД) уже лежит в
репозитории, поэтому клон запускается сразу. Перед выходом в боевой контур
замените значения и уберите файл из индекса:
`git update-index --skip-worktree .env` (образец всех параметров — `.env.example`).

## Проверки

```powershell
venv\Scripts\python.exe scripts\smoke_test.py        # сквозной сценарий на моках
venv\Scripts\python.exe tests\test_imports.py        # импорт всех модулей
venv\Scripts\python.exe tests\test_mock_api.py       # HTTP-API мок-сервера Яндекс 360
venv\Scripts\python.exe tests\test_web_pages.py      # рендер всех 9 страниц консоли
venv\Scripts\python.exe tests\test_services.py       # бизнес-логика сервисов
```

`smoke_test.py` прогоняет полный сценарий на моках: схема БД, справочник
сотрудников, сбор данных, отчёт о расхождениях, план отделов, DRY-RUN
синхронизации, обнаружение уволенных, события архивации, снимок ящика (вручную
и уволенного), правила хранения, аудит, sha256-проверку целостности снимков.

Остальные наборы в `tests\` требуют поднятого стенда (`start-stack.bat`) и
покрывают: сопоставление AD ↔ Яндекс 360 (логин / СНИЛС / employeeId / email),
сроки хранения по должностям, снимки и контроль sha256, экспорт XLSX, экспорт
писем EML/MBOX, маппинги и аудит в БД, события архивации, увольнения, моки
AD/IMAP, HTTP-маршруты и серверный рендер всех страниц консоли.

Аудит иконок и снимки страниц (нужен Chrome с `--remote-debugging-port=9222`):

```powershell
venv\Scripts\python.exe tests\collect_icons.py   # собрать имена иконок из web/
node tests\cdp_iconaudit.mjs                     # проверить их наличие в шрифте
node tests\cdp_screenshots.mjs                   # обновить docs\renders\*.png
```

Отчёт о последнем прогоне и рендеры страниц — в `tests\ОТЧЁТ-тестирование.md`
и `docs\renders\`.

## Переход в боевой режим

1. Заполнить `.env` по образцу `.env.example` (токен Яндекс 360, LDAP, PostgreSQL).
2. Указать реальные значения вместо моков: `AD_SERVER` — хост контроллера домена,
   `IMAP_HOST=imap.yandex.ru`, а `YANDEX_API_BASE=https://api360.yandex.net`
   (именно так, не пустым: пустое значение трактуется как мок и включает
   индикатор «мок» в интерфейсе).
3. Перезапустить приложение — код не меняется.

## Развёртывание на сервере (Linux)

Боевой контур живёт в `/opt/y360-admin`, БД `y360_admin` — в существующем
PostgreSQL, снимки ящиков — на общем хранилище (`BACKUP_ROOT`).

```bash
git clone <репозиторий> /opt/y360-admin      # или распаковать архив
cd /opt/y360-admin
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env                          # заполнить боевые значения
venv/bin/python db/init_db.py                 # применить схему БД

# перенос данных из проектов 08 (yandex_sync) и 16 (yandex_mailbox)
venv/bin/python scripts/migrate_legacy.py --dry-run   # проверка
venv/bin/python scripts/migrate_legacy.py             # перенос (идемпотентно)

# сервис
sudo cp deploy/y360-admin.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now y360-admin
curl -I http://127.0.0.1:8080/                # консоль отвечает
```

`deploy/y360-admin.service` запускает консоль от непривилегированного
пользователя и **не стартует без смонтированного `BACKUP_ROOT`**
(`RequiresMountsFor`): без этой проверки приложение создало бы каталог
хранилища на локальном диске и залило его снимками.

Проверка боевых подключений (Яндекс 360 API, Active Directory) без изменения
данных — все шаги синхронизации в режиме DRY-RUN:

```bash
venv/bin/python scripts/check_production.py
```

`scripts/migrate_legacy.py` переносит `users`, `backup_runs`, `dismissals`,
`backup_logs`, `sync_events`, `settings` из БД проекта 16 и `users_snapshot`,
`audit_log`, `department_mapping`, `organization_mapping` из БД проекта 08,
сверяет файловое хранилище с таблицей `backup_runs` (снимки на диске, которых
нет в БД, добавляются по `manifest.json`) и заполняет `dismissals.position` /
`retention_rule`. Скрипт идемпотентный — его можно запускать повторно, пока
старый и новый контур работают параллельно.

## Конфигурация

Основные настройки — в `.env` (см. `.env.example`, там описан каждый параметр).
Часть значений переопределяется из UI и хранится в таблице `settings`
(например, срок хранения снимков и ящик-хранилище для аудита).

## Структура проекта

```
y360-admin/
├── web/                 # NiceGUI-консоль
│   ├── theme.py         #   тёмная тема, CSS-классы y-*, палитра
│   ├── components.py    #   карточки, таблицы, диалоги, фоновые задачи
│   ├── layout.py        #   шапка, боковое меню, каркас страницы
│   ├── pages/           #   dashboard, sync, mismatches, users, backup,
│   │                    #   restore, retention, logs, settings
│   └── main.py          #   маршруты и запуск
├── core/                # конфиг, БД, модели, LDAP-клиент (боевой + мок), сравнение
├── services/            # бизнес-логика: pipeline, tasks, backup, restore,
│                        # retention, dismissals, storage, state, ...
├── integrations/        # HTTP-клиенты Яндекс 360 (Directory/Mail), токены IMAP
├── mocks/               # мок-сервер API Яндекс 360
├── db/                  # schema.sql + init_db.py
├── scripts/             # start-stack.bat, stop-stack.bat, smoke_test.py,
│                        # migrate_legacy.py (перенос из проектов 08/16),
│                        # check_production.py (проверка боевых подключений)
├── deploy/              # y360-admin.service (systemd)
├── tests/               # проверки (импорт, мок-API, страницы, сервисы, иконки)
│                        # + ОТЧЁТ-тестирование.md
├── docs/renders/        # рендеры страниц консоли (скриншоты 1600 px)
├── tools/               # портируемый PostgreSQL 17 (pgsql/bin, lib, share)
└── data/                # снимки (backups/), логи (logs/), БД (pgdata/), orgs.json
```

## Жизненный цикл ящика

```
AD (без СНИЛС) → «только в Яндекс 360» → событие архивации → снимок ящика
(manifest.json, index.jsonl, mail.mbox, tar.gz) → срок по должности → удаление
```

Каждый шаг журналируется: `audit_log` (операции), `backup_logs` (ход бэкапа),
`data/logs/y360-admin.log` (приложение).

## Требования

- Windows + Python 3.12, PostgreSQL 17 (в комплекте `tools\pg17`)
- Зависимости: `requirements.txt` — NiceGUI, psycopg2, requests, ldap3,
  python-dotenv, openpyxl
