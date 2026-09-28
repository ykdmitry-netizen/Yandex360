"""Инициализация схемы PostgreSQL (идемпотентно).

Создаёт/дополняет все таблицы единой БД консоли (db/schema.sql) и выводит
сводку. Запуск:  venv\\Scripts\\python.exe db\\init_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import get_settings  # noqa: E402
from core.db import init_schema, query_all  # noqa: E402
from core.logs import get_logger  # noqa: E402

logger = get_logger("db.init")


def main() -> int:
    cfg = get_settings()
    print(f"PostgreSQL: {cfg.pg_host}:{cfg.pg_port}/{cfg.pg_database} (user {cfg.pg_user})")
    try:
        init_schema()
    except Exception as exc:  # noqa: BLE001
        print(f"ОШИБКА: не удалось применить db/schema.sql: {exc}")
        return 1

    tables = query_all(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' ORDER BY table_name"
    )
    counts = {}
    for name in ("users", "dismissals", "sync_events", "retention_rules", "backup_runs"):
        try:
            row = query_all(f"SELECT count(*) AS n FROM {name}")
            counts[name] = row[0]["n"] if row else 0
        except Exception:  # noqa: BLE001
            counts[name] = "?"
    print(f"Таблиц в БД: {len(tables)}: " + ", ".join(t['table_name'] for t in tables))
    print("Строк: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
