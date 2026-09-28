"""Единый слой PostgreSQL для всего проекта (одна БД y360_admin).

Пул соединений (thread-safe), инициализация схемы, обёртки запросов и
доменные функции: снимки AD/Яндекс, аудит-трейл, маппинги отделов
и организаций. Все сервисы ходят в БД только через этот модуль.
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from psycopg2 import pool

from core.config import get_settings
from core.logs import get_logger

logger = get_logger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

_pool: pool.ThreadedConnectionPool | None = None


def init_schema() -> None:
    """Создаёт таблицы из db/schema.sql (идемпотентно)."""
    cfg = get_settings()
    with psycopg2.connect(**cfg.pg_params) as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    logger.info("Схема БД применена (db/schema.sql)")


def _get_pool() -> pool.ThreadedConnectionPool:
    """Лениво создаёт пул соединений (min=2, max=20)."""
    global _pool
    if _pool is None:
        cfg = get_settings()
        _pool = pool.ThreadedConnectionPool(2, 20, **cfg.pg_params)
    return _pool


def db_available() -> bool:
    """Быстрая проба: доступна ли PostgreSQL (для индикаторов UI)."""
    try:
        row = query_one("SELECT 1 AS ok")
        return bool(row)
    except Exception as e:
        logger.warning(f"PostgreSQL недоступен: {e}")
        return False


@contextlib.contextmanager
def get_conn():
    """Соединение из пула (с короткими ретраями), commit/rollback, возврат."""
    p = _get_pool()
    last_error: Exception | None = None
    conn = None
    for attempt in range(3):
        try:
            conn = p.getconn()
            break
        except psycopg2.OperationalError as exc:
            last_error = exc
            time.sleep(1 + attempt * 2)
    else:
        raise last_error or psycopg2.OperationalError("PostgreSQL недоступен")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)


def query_all(sql: str, params: Iterable[Any] = ()) -> List[dict]:
    """SELECT -> список словарей."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, list(params))
            return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    rows = query_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """INSERT/UPDATE/DELETE, возвращает число затронутых строк."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, list(params))
            return cur.rowcount


def execute_returning(sql: str, params: Iterable[Any] = ()) -> Any:
    """INSERT ... RETURNING, возвращает первое значение."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, list(params))
            result = cur.fetchone()
        return result[0] if result else None


# ------------------------------------------------------------
# Снимок пользователей (AD / Яндекс / отделы)
# ------------------------------------------------------------

def save_snapshot(yandex_users: list, ad_users: list, departments: Optional[dict] = None) -> bool:
    """Атомарная запись снимка пользователей в БД."""
    departments = departments or {}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users_snapshot")
                upsert = (
                    "INSERT INTO users_snapshot (login, source, data) VALUES (%s,%s,%s) "
                    "ON CONFLICT (login, source) DO UPDATE SET data = EXCLUDED.data, "
                    "snapshot_at = NOW()"
                )
                for u in yandex_users:
                    cur.execute(upsert, (u.nickname, 'yandex', json.dumps(u.to_dict(), ensure_ascii=False)))
                for u in ad_users:
                    cur.execute(upsert, (u.sam_account_name, 'ad', json.dumps(u.to_dict(), ensure_ascii=False)))
                cur.execute(upsert, ("__departments__", 'dept', json.dumps(departments, ensure_ascii=False)))
        logger.info(f"Снимок сохранён в БД: {len(yandex_users)} Яндекс + {len(ad_users)} AD")
        return True
    except Exception as e:
        logger.warning(f"Не удалось сохранить снимок в БД: {e}")
        return False


def load_snapshot() -> Optional[Tuple[list, list, dict]]:
    """Возвращает (yandex_users, ad_users, departments) из БД или None."""
    from core.models import YandexUser, ADUser

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT login, source, data FROM users_snapshot "
                    "WHERE source IN ('yandex','ad') ORDER BY login"
                )
                rows = cur.fetchall()
                yandex_users = []
                ad_users = []
                for row in rows:
                    if row["source"] == "yandex":
                        yandex_users.append(YandexUser.from_dict(row["data"]))
                    elif row["source"] == "ad":
                        ad_users.append(ADUser.from_dict(row["data"]))
                departments = {}
                cur.execute(
                    "SELECT data FROM users_snapshot WHERE source='dept' AND login='__departments__'"
                )
                drow = cur.fetchone()
                if drow and drow["data"]:
                    departments = {int(k): v for k, v in drow["data"].items()}
        return yandex_users, ad_users, departments
    except Exception as e:
        logger.warning(f"Не удалось загрузить снимок из БД: {e}")
        return None


def snapshot_age_minutes() -> Optional[float]:
    """Сколько минут назад сделан последний снимок (для индикатора свежести)."""
    try:
        row = query_one("SELECT MAX(snapshot_at) AS ts FROM users_snapshot")
        if not row or not row["ts"]:
            return None
        return (time.time() - row["ts"].timestamp()) / 60.0
    except Exception:
        return None


# ------------------------------------------------------------
# Аудит-трейл
# ------------------------------------------------------------

def log_audit(operation: str, target_login: Optional[str] = None,
              before_data: Any = None, after_data: Any = None,
              success: bool = True, error: Optional[str] = None,
              dry_run: bool = False, performed_by: Optional[str] = None) -> bool:
    """Записывает изменение в audit_log. Возвращает True при успехе."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_log
                    (operation, target_login, before_data, after_data,
                     performed_by, dry_run, success, error_message)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (operation, target_login,
                     json.dumps(before_data, ensure_ascii=False) if before_data else None,
                     json.dumps(after_data, ensure_ascii=False) if after_data else None,
                     performed_by, dry_run, success, error)
                )
        return True
    except Exception as e:
        logger.warning(f"Не удалось записать аудит: {e}")
        return False


def get_audit_log(limit: int = 500, operation: Optional[str] = None,
                  only_failed: bool = False, search: Optional[str] = None) -> List[dict]:
    """Последние записи аудита (журнал UI с фильтрами)."""
    sql = (
        "SELECT id, created_at, operation, target_login, success, error_message, "
        "performed_by, dry_run FROM audit_log WHERE TRUE"
    )
    params: List[Any] = []
    if operation:
        sql += " AND operation = %s"
        params.append(operation)
    if only_failed:
        sql += " AND success = FALSE"
    if search:
        sql += " AND target_login ILIKE %s"
        params.append(f"%{search}%")
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(int(limit))
    try:
        return query_all(sql, params)
    except Exception as e:
        logger.warning(f"Не удалось прочитать аудит-лог: {e}")
        return []


# ------------------------------------------------------------
# Маппинг отделов AD -> Яндекс
# ------------------------------------------------------------

def get_department_mapping() -> List[dict]:
    try:
        return query_all(
            "SELECT id, ad_department, yandex_department_id, "
            "yandex_department_name, confirmed FROM department_mapping "
            "ORDER BY confirmed DESC, ad_department"
        )
    except Exception as e:
        logger.warning(f"Не удалось прочитать маппинг отделов: {e}")
        return []


def set_department_mapping(ad_department: str, yandex_department_id=None,
                           yandex_department_name=None, confirmed: bool = False) -> bool:
    try:
        execute(
            """
            INSERT INTO department_mapping
            (ad_department, yandex_department_id, yandex_department_name, confirmed)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (ad_department)
            DO UPDATE SET yandex_department_id = EXCLUDED.yandex_department_id,
                          yandex_department_name = EXCLUDED.yandex_department_name,
                          confirmed = EXCLUDED.confirmed
            """,
            (ad_department, yandex_department_id, yandex_department_name, confirmed)
        )
        return True
    except Exception as e:
        logger.warning(f"Не удалось обновить маппинг отдела: {e}")
        return False


def delete_department_mapping(mapping_id: int) -> bool:
    try:
        execute("DELETE FROM department_mapping WHERE id=%s", (int(mapping_id),))
        return True
    except Exception as e:
        logger.warning(f"Не удалось удалить маппинг отдела: {e}")
        return False


# ------------------------------------------------------------
# Маппинг организаций (заменяет orgs.json)
# ------------------------------------------------------------

def get_organization_mapping() -> List[dict]:
    try:
        return query_all("SELECT id, short_name, full_name FROM organization_mapping ORDER BY short_name")
    except Exception as e:
        logger.warning(f"Не удалось прочитать маппинг организаций: {e}")
        return []


def set_organization_mapping(short_name: str, full_name: str) -> bool:
    try:
        execute(
            """
            INSERT INTO organization_mapping (short_name, full_name)
            VALUES (%s,%s)
            ON CONFLICT (short_name) DO UPDATE SET full_name = EXCLUDED.full_name
            """,
            (short_name, full_name)
        )
        return True
    except Exception as e:
        logger.warning(f"Не удалось обновить маппинг организации: {e}")
        return False
