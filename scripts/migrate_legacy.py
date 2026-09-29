#!/usr/bin/env python3
"""Перенос данных из легаси-проектов в единую БД y360_admin.

Источники (по умолчанию — боевые пути на сервере):
  * /opt/yandex_mailbox/.env                    — БД проекта 16 (yandex_mailbox)
  * /opt/yandex_sync/.streamlit/secrets.toml    — БД проекта 08 (yandex_sync)

Что переносится:
  из 16: settings, users, backup_runs, dismissals, backup_logs, sync_events
  из 08: users_snapshot, audit_log, department_mapping, organization_mapping

Дополнительно:
  * сверка файлового хранилища (BACKUP_ROOT) с backup_runs — снимки, которые
    есть на диске, но не записаны в БД, добавляются по данным manifest.json;
    незавершённые каталоги (без manifest/архива) перечисляются в отчёте;
  * backfill полей dismissals.position / retention_rule, если они пустые.

Скрипт идемпотентный: повторный запуск не дублирует данные и не затирает
значения, которые новый проект пишет сам (position, snils у users).

Запуск:
    venv/bin/python scripts/migrate_legacy.py --dry-run
    venv/bin/python scripts/migrate_legacy.py
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402
from psycopg2.extras import execute_values  # noqa: E402

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

# ---------------------------------------------------------------- источники

def read_kv(path: Path) -> dict[str, str]:
    """Простые KEY=VALUE из .env."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _find_key(tree, wanted: str):
    stack = [tree]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key, value in cur.items():
                if key.lower() == wanted.lower() and not isinstance(value, (dict, list)):
                    return value
                if isinstance(value, dict):
                    stack.append(value)
    return None


def mailbox_dsn(env_path: Path) -> str:
    env = read_kv(env_path)
    host = env.get("PG_HOST") or env.get("PGHOST") or "127.0.0.1"
    port = env.get("PG_PORT") or env.get("PGPORT") or "5432"
    db = env.get("PG_DATABASE") or env.get("PGDATABASE") or "yandex_mailbox"
    user = env.get("PG_USER") or env.get("PGUSER") or "mailbox_app"
    pw = env.get("PG_PASSWORD") or env.get("PGPASSWORD") or ""
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def sync_dsn(secrets_path: Path) -> str:
    """DSN проекта 08: креды берём строго из секции [postgres].

    В secrets.toml есть ещё секция [sync_events] — это БД проекта 16
    (кросс-проектный обмен), её брать нельзя.
    """
    if tomllib is None:
        raise SystemExit("нужен Python 3.11+ (tomllib) для чтения secrets.toml")
    tree = tomllib.loads(secrets_path.read_text(encoding="utf-8"))
    sections = {k.lower(): v for k, v in tree.items() if isinstance(v, dict)}
    pg = sections.get("postgres", {})
    host = pg.get("host") or _find_key(tree, "host") or "127.0.0.1"
    port = pg.get("port") or _find_key(tree, "port") or "5432"
    db = pg.get("database") or "yandex_sync"
    user = pg.get("user") or "sync_app"
    pw = pg.get("password") or ""
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def target_dsn() -> str:
    from core.config import get_settings

    cfg = get_settings()
    return (f"postgresql://{cfg.pg_user}:{cfg.pg_password}@"
            f"{cfg.pg_host}:{cfg.pg_port}/{cfg.pg_database}")


# ---------------------------------------------------------------- перенос

# (таблица, ключ конфликта, обновлять ли существующие строки)
MAILBOX_TABLES = [
    # настройки переносим значениями легаси: они и есть рабочие
    ("settings", "key", ("value",)),
    ("users", "id", ("nickname", "name", "department_id", "email", "status", "updated_at")),
    ("backup_runs", "id", None),
    ("dismissals", "user_id", None),
    ("backup_logs", "id", None),
    # пустой target: пропускаем при конфликте по любому уникальному ключу
    ("sync_events", "", None),
]
SYNC_TABLES = [
    ("users_snapshot", "login,source", "newer"),
    ("audit_log", "id", None),
    ("department_mapping", "ad_department", None),
    ("organization_mapping", "short_name", None),
]
SEQUENCE_TABLES = ["backup_runs", "backup_logs", "sync_events", "audit_log",
                   "department_mapping", "organization_mapping", "retention_rules"]

# Источники событий в легаси-БД. События переносятся с чужим source, а наш
# пайплайн публикует под своим — из-за UNIQUE(source, user_id) это создавало
# дубли (59 событий превращались в 118), поэтому source нормализуем.
LEGACY_EVENT_SOURCES = ("project08", "project16", "mailbox", "yandex_sync", "yandex_mailbox")


def drop_excluded_rows(dst, dry_run: bool) -> dict:
    """Убирает из приёмника строки исключённых (сервисных) ящиков.

    Легаси-БД продолжает их хранить, поэтому без этой чистки каждая
    миграция возвращала бы сервисные ящики в очередь архивации.
    """
    from services.filters import excluded_logins

    logins = sorted(excluded_logins())
    if not logins:
        return {"removed": 0, "logins": []}
    removed = 0
    with dst.cursor() as cur:
        for table in ("sync_events", "dismissals"):
            if dry_run:
                cur.execute(f"SELECT count(*) FROM {table} WHERE login = ANY(%s)", (logins,))
                removed += cur.fetchone()[0]
            else:
                cur.execute(f"DELETE FROM {table} WHERE login = ANY(%s)", (logins,))
                removed += cur.rowcount
    return {"removed": removed, "logins": logins}


def normalize_event_sources(dst, dry_run: bool) -> dict:
    from services.integration import SOURCE

    with dst.cursor() as cur:
        cur.execute("SELECT count(*) FROM sync_events WHERE source = ANY(%s)",
                    (list(LEGACY_EVENT_SOURCES),))
        legacy = cur.fetchone()[0]
        if legacy and not dry_run:
            cur.execute("UPDATE sync_events SET source = %s WHERE source = ANY(%s)",
                        (SOURCE, list(LEGACY_EVENT_SOURCES)))
        cur.execute("SELECT count(*) FROM (SELECT user_id FROM sync_events "
                    "GROUP BY user_id HAVING count(*) > 1) t")
        duplicates = cur.fetchone()[0]
    return {"legacy_renamed": legacy, "duplicates_left": duplicates, "source": SOURCE}


def columns_of(cur, table: str) -> list[str]:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return [r[0] for r in cur.fetchall()]


def json_columns(cur, table: str) -> set[str]:
    """JSON/JSONB-колонки: psycopg2 отдаёт их словарями, обратно нужен Json()."""
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s AND data_type IN ('json', 'jsonb')", (table,))
    return {r[0] for r in cur.fetchall()}


def copy_table(src, dst, table: str, conflict: str, update_cols, dry_run: bool) -> dict:
    with src.cursor() as scur:
        src_cols = columns_of(scur, table)
        if not src_cols:
            return {"table": table, "skipped": "нет таблицы в источнике"}
        scur.execute(f"SELECT {', '.join(src_cols)} FROM {table}")
        rows = scur.fetchall()
    with dst.cursor() as dcur:
        dst_cols = set(columns_of(dcur, table))
        if not dst_cols:
            return {"table": table, "skipped": "нет таблицы в приёмнике"}
        cols = [c for c in src_cols if c in dst_cols]
        if not cols:
            return {"table": table, "skipped": "нет общих колонок"}
        json_cols = json_columns(dcur, table)
        idx = [src_cols.index(c) for c in cols]
        values = []
        for row in rows:
            prepared = []
            for c, i in zip(cols, idx):
                value = row[i]
                if value is not None and c in json_cols and isinstance(value, (dict, list)):
                    value = psycopg2.extras.Json(value)
                prepared.append(value)
            values.append(tuple(prepared))
        if not values:
            return {"table": table, "copied": 0, "source": 0}

        if update_cols == "newer":
            conflict_sql = (f"ON CONFLICT ({conflict}) DO UPDATE SET "
                            f"data = EXCLUDED.data, snapshot_at = EXCLUDED.snapshot_at "
                            f"WHERE EXCLUDED.snapshot_at > users_snapshot.snapshot_at")
        elif update_cols:
            sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
            conflict_sql = f"ON CONFLICT ({conflict}) DO UPDATE SET {sets}"
        else:
            conflict_sql = (f"ON CONFLICT ({conflict}) DO NOTHING" if conflict
                             else "ON CONFLICT DO NOTHING")

        sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s {conflict_sql}")
        if dry_run:
            dcur.execute(f"SELECT count(*) FROM {table}")
            before = dcur.fetchone()[0]
            return {"table": table, "source": len(values), "before": before, "dry_run": True}
        execute_values(dcur, sql, values, page_size=500)
        dcur.execute(f"SELECT count(*) FROM {table}")
        return {"table": table, "source": len(values), "after": dcur.fetchone()[0]}


def fix_sequences(dst, dry_run: bool) -> list[str]:
    fixed = []
    if dry_run:
        return fixed
    with dst.cursor() as cur:
        for table in SEQUENCE_TABLES:
            cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table,))
            row = cur.fetchone()
            if not row or not row[0]:
                continue
            cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
            max_id = cur.fetchone()[0]
            cur.execute("SELECT setval(%s, %s, %s)", (row[0], max(max_id, 1), max_id > 0))
            fixed.append(f"{table} → {max_id}")
    return fixed


def backfill_dismissals(dst, dry_run: bool) -> int:
    """Заполняет position/retention_rule у перенесённых увольнений."""
    from services import retention
    with dst.cursor() as cur:
        cur.execute("SELECT d.user_id, u.position FROM dismissals d "
                    "LEFT JOIN users u ON u.id = d.user_id "
                    "WHERE d.position IS NULL")
        rows = cur.fetchall()
    filled = 0
    for user_id, position in rows:
        plan = retention.plan_deletion(position)
        rule = plan.get("rule")
        if dry_run:
            filled += 1
            continue
        with dst.cursor() as cur:
            cur.execute("UPDATE dismissals SET position = %s, retention_rule = %s WHERE user_id = %s",
                        (position, rule.name if rule else None, user_id))
        filled += 1
    return filled


# ------------------------------------------------- хранилище снимков

def reconcile_storage(dst, backup_root: Path, org_id: str, dry_run: bool) -> dict:
    """Добавляет в backup_runs снимки, которые лежат на диске, но не в БД."""
    if not backup_root.exists():
        return {"error": f"нет каталога {backup_root}"}
    org_dir = backup_root / org_id
    added, incomplete, present = [], [], 0
    with dst.cursor() as cur:
        cur.execute("SELECT path FROM backup_runs WHERE path IS NOT NULL")
        known = {r[0] for r in cur.fetchall()}

    for login_dir in sorted(p for p in org_dir.iterdir() if p.is_dir()):
        for snap in sorted(p for p in login_dir.iterdir() if p.is_dir()):
            archive = snap.with_suffix(".tar.gz")
            manifest_path = snap / "manifest.json"
            if str(snap) in known:
                present += 1
                continue
            if not archive.exists() or not manifest_path.exists():
                incomplete.append({"login": login_dir.name, "stamp": snap.name,
                                   "mbox_bytes": (snap / "mail.mbox").stat().st_size
                                   if (snap / "mail.mbox").exists() else 0})
                continue
            try:
                man = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                incomplete.append({"login": login_dir.name, "stamp": snap.name, "error": str(exc)})
                continue
            created = man.get("created_at")
            try:
                started = datetime.fromisoformat(created) if created else datetime.now(timezone.utc)
            except ValueError:
                started = datetime.now(timezone.utc)
            added.append((org_id, login_dir.name, man.get("reason", "manual"), started, started,
                          "success", int(man.get("messages") or 0), int(man.get("size_bytes") or 0),
                          man.get("archive_sha256"), str(snap), None))

    if added and not dry_run:
        with dst.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO backup_runs (org_id, login, reason, started_at, finished_at, status, "
                "messages, size_bytes, sha256, path, error) VALUES %s "
                "ON CONFLICT DO NOTHING",
                added, page_size=200)
    return {"added": len(added), "already": present, "incomplete": incomplete}


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mailbox-env", default="/opt/yandex_mailbox/.env")
    ap.add_argument("--sync-secrets", default="/opt/yandex_sync/.streamlit/secrets.toml")
    ap.add_argument("--mailbox-dsn", default="")
    ap.add_argument("--sync-dsn", default="")
    ap.add_argument("--skip-storage", action="store_true", help="не сверять файловое хранилище")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from core.config import get_settings

    cfg = get_settings()
    src_mail = args.mailbox_dsn or mailbox_dsn(Path(args.mailbox_env))
    src_sync = args.sync_dsn or sync_dsn(Path(args.sync_secrets))
    dst_dsn = target_dsn()

    print(f"Приёмник:  {cfg.pg_host}:{cfg.pg_port}/{cfg.pg_database}")
    print(f"Источник 16 (mailbox): {src_mail.split('@')[-1]}")
    print(f"Источник 08 (sync):    {src_sync.split('@')[-1]}")
    if args.dry_run:
        print("РЕЖИМ ПРОВЕРКИ (--dry-run): изменения не записываются\n")

    conn_dst = psycopg2.connect(dst_dsn)
    conn_dst.autocommit = False
    conn_mail = psycopg2.connect(src_mail)
    conn_sync = psycopg2.connect(src_sync)

    report: dict = {"tables": [], "sequences": [], "storage": {}, "dismissals_filled": 0}
    try:
        for conn, tables, label in ((conn_mail, MAILBOX_TABLES, "16 yandex_mailbox"),
                                    (conn_sync, SYNC_TABLES, "08 yandex_sync")):
            print(f"--- {label} ---")
            for table, conflict, update_cols in tables:
                res = copy_table(conn, conn_dst, table, conflict, update_cols, args.dry_run)
                report["tables"].append(res)
                print(f"   {table:<22} {res}")
            conn.rollback()  # только чтение

        report["excluded_rows"] = drop_excluded_rows(conn_dst, args.dry_run)
        report["event_sources"] = normalize_event_sources(conn_dst, args.dry_run)
        print(f"--- события ---\n   {report['event_sources']}")

        report["sequences"] = fix_sequences(conn_dst, args.dry_run)
        report["dismissals_filled"] = backfill_dismissals(conn_dst, args.dry_run)

        if not args.skip_storage:
            print("--- файловое хранилище ---")
            report["storage"] = reconcile_storage(conn_dst, cfg.backup_root, cfg.yandex_org_id, args.dry_run)
            st = report["storage"]
            if "error" in st:
                print(f"   {st['error']}")
            else:
                print(f"   добавлено снимков в БД: {st['added']}, уже было: {st['already']}, "
                      f"незавершённых каталогов: {len(st['incomplete'])}")
                for row in st["incomplete"][:15]:
                    print(f"      не завершён: {row['login']:<22} {row['stamp']}  "
                          f"mbox {row.get('mbox_bytes', 0)/2**20:.0f} МБ")

        if args.dry_run:
            conn_dst.rollback()
            print("\n--dry-run: откат, ничего не изменено")
        else:
            conn_dst.commit()
            print("\nИзменения зафиксированы.")

        with conn_dst.cursor() as cur:
            for table in ("users", "backup_runs", "dismissals", "sync_events",
                          "audit_log", "users_snapshot", "backup_logs",
                          "department_mapping", "organization_mapping"):
                cur.execute(f"SELECT count(*) FROM {table}")
                print(f"   в y360_admin {table:<22} {cur.fetchone()[0]}")
    finally:
        conn_mail.close()
        conn_sync.close()
        conn_dst.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
