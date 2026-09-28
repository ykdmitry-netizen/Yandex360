-- ============================================================
-- Y360 Admin — единая схема БД (объединение проектов 08 и 16).
-- База: y360_admin, пользователь: y360_app.
-- Применение: python -m db.init_db  (или psql -f db/schema.sql)
-- Все блоки идемпотентны (IF NOT EXISTS / ON CONFLICT DO NOTHING).
-- ============================================================

-- ------------------------------------------------------------
-- Настройки приложения (key/value)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ------------------------------------------------------------
-- Пользователи организации (из Directory API Яндекс 360)
-- position нужна для расчёта срока хранения (retention_rules).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,
    org_id         TEXT NOT NULL,
    login          TEXT NOT NULL,
    nickname       TEXT,
    name           TEXT,
    position       TEXT,
    department_id  TEXT,
    email          TEXT,
    snils          TEXT,
    status         TEXT DEFAULT 'active',
    updated_at     TIMESTAMPTZ DEFAULT now()
);

-- ------------------------------------------------------------
-- Запуски бэкапов (в т.ч. снимки уволенных)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backup_runs (
    id            BIGSERIAL PRIMARY KEY,
    org_id        TEXT NOT NULL,
    login         TEXT NOT NULL,
    reason        TEXT DEFAULT 'manual',   -- manual / scheduled / dismissal / pre_delete
    started_at    TIMESTAMPTZ DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT DEFAULT 'running',   -- running / success / error
    messages      BIGINT DEFAULT 0,
    size_bytes    BIGINT DEFAULT 0,
    sha256        TEXT,                     -- хеш итогового .tar.gz
    path          TEXT,
    error         TEXT
);

-- ------------------------------------------------------------
-- Жизненный цикл уволенного сотрудника
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dismissals (
    user_id                  TEXT PRIMARY KEY REFERENCES users(id),
    login                    TEXT NOT NULL,
    org_id                   TEXT NOT NULL,
    fired_at                 TIMESTAMPTZ,
    backup_done_at           TIMESTAMPTZ,
    backup_run_id            BIGINT REFERENCES backup_runs(id),
    deletion_scheduled_at    TIMESTAMPTZ,   -- когда планируется удалить ящик
    deletion_done_at         TIMESTAMPTZ,   -- фактическое удаление
    retention_until          TIMESTAMPTZ,   -- до какого срока хранить бэкап
    position                 TEXT,          -- должность на момент снимка
    retention_rule           TEXT,          -- применённое правило
    notes                    TEXT
);

-- ------------------------------------------------------------
-- Логи операций бэкапа
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backup_logs (
    id         BIGSERIAL PRIMARY KEY,
    run_id     BIGINT REFERENCES backup_runs(id),
    ts         TIMESTAMPTZ DEFAULT now(),
    level      TEXT,
    message    TEXT
);

-- ------------------------------------------------------------
-- События «только в Яндексе» (кандидаты на увольнение).
-- Пайплайн публикует их, модуль архивации принимает и ведёт статусы:
--   new -> accepted -> backed_up -> delete_scheduled -> deleted
--   (или dismissed, если человек вернулся в AD)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_events (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL DEFAULT 'y360-admin',
    user_id      TEXT NOT NULL,                       -- id в Яндекс 360
    login        TEXT NOT NULL,
    email        TEXT,
    display_name TEXT,
    event_type   TEXT NOT NULL DEFAULT 'only_in_yandex',
    status       TEXT NOT NULL DEFAULT 'new',
    created_at   TIMESTAMPTZ DEFAULT now(),
    processed_at TIMESTAMPTZ,
    notes        TEXT,
    UNIQUE (source, user_id)
);

-- ------------------------------------------------------------
-- Правила хранения ящика до удаления по должности (ТЗ п.3).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS retention_rules (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    keywords            TEXT NOT NULL DEFAULT '',   -- через запятую; пусто = Fallback
    delete_after_amount INTEGER NOT NULL,
    delete_after_unit   TEXT NOT NULL DEFAULT 'days',   -- days | months
    priority            INTEGER NOT NULL DEFAULT 100,   -- меньше — выше приоритет
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    notes               TEXT
);

-- ------------------------------------------------------------
-- Снимок пользователей AD/Яндекс/отделы (заменяет cache.json)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users_snapshot (
    login VARCHAR(200) NOT NULL,
    source VARCHAR(10) NOT NULL,   -- 'yandex' / 'ad' / 'dept' (отделы)
    data JSONB NOT NULL,
    snapshot_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (login, source)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_source ON users_snapshot(source);

-- ------------------------------------------------------------
-- Аудит-трейл (жёсткое логирование всех действий)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    operation VARCHAR(50) NOT NULL,   -- update_user / update_contacts / create_dept / backup / restore ...
    target_login VARCHAR(200),
    before_data JSONB,
    after_data JSONB,
    performed_by VARCHAR(200),
    dry_run BOOLEAN DEFAULT FALSE,
    success BOOLEAN,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

-- ------------------------------------------------------------
-- Маппинг отделов AD -> Яндекс
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS department_mapping (
    id SERIAL PRIMARY KEY,
    ad_department VARCHAR(500) NOT NULL UNIQUE,
    yandex_department_id INTEGER,
    yandex_department_name VARCHAR(500),
    confirmed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ------------------------------------------------------------
-- Маппинг организаций (заменяет orgs.json)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS organization_mapping (
    id SERIAL PRIMARY KEY,
    short_name VARCHAR(200) NOT NULL UNIQUE,
    full_name VARCHAR(500) NOT NULL
);

-- ------------------------------------------------------------
-- Индексы для частых запросов
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_users_org ON users(org_id);
CREATE INDEX IF NOT EXISTS idx_backup_runs_login ON backup_runs(login, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_backup_logs_run ON backup_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_dismissals_backup_done ON dismissals(backup_done_at);
CREATE INDEX IF NOT EXISTS idx_sync_events_status ON sync_events(status);
CREATE INDEX IF NOT EXISTS idx_sync_events_login ON sync_events(login);

-- ------------------------------------------------------------
-- Дефолтные настройки (перезаписываются через .env / UI)
-- ------------------------------------------------------------
INSERT INTO settings (key, value) VALUES
    ('retention_days', '365'),             -- сколько хранить снимок уволенного
    ('delete_mailbox_after_days', '90'),   -- fallback-срок до планирования удаления
    ('restore_available_to', 'admin')      -- кто имеет доступ к бэкапам
ON CONFLICT (key) DO NOTHING;

-- ------------------------------------------------------------
-- Дефолтные правила retention (таблица из ТЗ п.3)
-- ------------------------------------------------------------
INSERT INTO retention_rules (name, keywords, delete_after_amount, delete_after_unit, priority, notes) VALUES
    ('Топ-менеджмент',
     'генеральный директор, исполнительный директор, директор, заместитель, президент, председатель',
     3, 'months', 10, 'Генеральный директор, директор, заместитель директора'),
    ('Руководители',
     'начальник управления, начальник отдела, начальник службы, руководитель управления, руководитель отдела, руководитель службы, начальник, руководитель, заведующий',
     2, 'months', 20, 'Начальники управлений, служб, отделов'),
    ('ИТ и цифра',
     'цифровой трансформаци, цифровизаци, информационн, ИТ, IT, системный администратор, программист',
     2, 'months', 25, 'Дирекция по цифровой трансформации'),
    ('Закупки и склад',
     'закупк, снабжен, склад, кладовщик, комплектовщик, логист',
     3, 'months', 30, 'Дирекция по закупкам, включая кладовщиков'),
    ('Остальные',
     '',
     14, 'days', 9999, 'Все прочие работники (правило по умолчанию)')
ON CONFLICT (name) DO NOTHING;
