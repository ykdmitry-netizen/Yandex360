"""Статусы бэкапов, логи операций и настройки в PostgreSQL."""
from __future__ import annotations

from typing import Optional

from core.db import execute, execute_returning, query_all, query_one
from core.logs import get_logger

logger = get_logger(__name__)


# ---------- Запуски бэкапов ----------

def start_run(org_id: str, login: str, reason: str = "manual") -> int:
    """Создаёт запись о запуске бэкапа, возвращает id."""
    return int(
        execute_returning(
            "INSERT INTO backup_runs (org_id, login, reason, status) VALUES (%s,%s,%s,'running') RETURNING id",
            (org_id, login, reason),
        )
    )


def finish_run(run_id: int, messages: int, size_bytes: int, path: str, sha256: str | None = None) -> None:
    execute(
        "UPDATE backup_runs SET status='success', finished_at=now(), messages=%s, size_bytes=%s, path=%s, sha256=%s WHERE id=%s",
        (messages, size_bytes, path, sha256, run_id),
    )


def fail_run(run_id: int, error: str) -> None:
    execute(
        "UPDATE backup_runs SET status='error', finished_at=now(), error=%s WHERE id=%s",
        (error[:2000], run_id),
    )


def log(run_id: Optional[int], level: str, message: str) -> None:
    execute(
        "INSERT INTO backup_logs (run_id, level, message) VALUES (%s,%s,%s)",
        (run_id, level, message[:4000]),
    )


def recent_runs(limit: int = 200) -> list[dict]:
    return query_all(
        "SELECT * FROM backup_runs ORDER BY started_at DESC LIMIT %s", (limit,)
    )


def runs_for_user(login: str, limit: int = 50) -> list[dict]:
    return query_all(
        "SELECT * FROM backup_runs WHERE login=%s ORDER BY started_at DESC LIMIT %s",
        (login, limit),
    )


def recent_logs(limit: int = 500) -> list[dict]:
    return query_all("SELECT * FROM backup_logs ORDER BY id DESC LIMIT %s", (limit,))


def last_status_by_login() -> dict[str, str]:
    """Логин -> статус последнего бэкапа."""
    rows = query_all(
        """
        SELECT DISTINCT ON (login) login, status
        FROM backup_runs
        ORDER BY login, started_at DESC
        """
    )
    return {r["login"]: r["status"] for r in rows}


# ---------- Настройки ----------

def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key=%s", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        """
        INSERT INTO settings (key, value) VALUES (%s,%s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """,
        (key, value),
    )
