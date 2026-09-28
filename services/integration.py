"""Интеграция «расхождения → архивация»: события жизненного цикла увольнения.

Пайплайн публикует обнаруженных пользователей «только в Яндексе» (нет пары
в AD — кандидаты на увольнение) как события sync_events. Модуль архивации
принимает их и ведёт жизненный цикл:

  new -> accepted -> backed_up -> delete_scheduled -> deleted
  (или dismissed, если человек вернулся в AD)

В объединённом проекте всё в одной БД — обмен мгновенный, без кросс-БД
подключений и без live-JOIN между двумя базами.
"""
from __future__ import annotations

from typing import List

from core.db import execute, query_all, query_one
from core.logs import get_logger
from services import dismissals

logger = get_logger(__name__)

SOURCE = "y360-admin"

SYNC_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_events (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL DEFAULT 'y360-admin',
    user_id      TEXT NOT NULL,
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
"""


def _ensure_table() -> None:
    """Гарантирует наличие таблицы sync_events (идемпотентно)."""
    execute(SYNC_EVENTS_SCHEMA)


# ------------------------------------------------------------
# Запись / обновление событий
# ------------------------------------------------------------

def upsert_event(
    user_id: str,
    login: str,
    *,
    source: str = SOURCE,
    email: str | None = None,
    display_name: str | None = None,
    status: str = "new",
    notes: str | None = None,
) -> None:
    """Создаёт или обновляет событие (по source+user_id). Не трогает processed_at.

    Статус существующих событий НЕ сбрасывается (backed_up/deleted остаются).
    """
    _ensure_table()
    execute(
        """
        INSERT INTO sync_events (source, user_id, login, email, display_name, status, notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (source, user_id) DO UPDATE SET
            login = EXCLUDED.login,
            email = COALESCE(EXCLUDED.email, sync_events.email),
            display_name = COALESCE(EXCLUDED.display_name, sync_events.display_name),
            notes = COALESCE(EXCLUDED.notes, sync_events.notes)
        """,
        (source, user_id, login, email, display_name, status, notes),
    )


def update_event_status(user_id: str, status: str, notes: str | None = None,
                        source: str = SOURCE) -> None:
    """Обновляет статус события (после бэкапа/удаления/возврата в AD)."""
    _ensure_table()
    if notes:
        execute(
            "UPDATE sync_events SET status=%s, processed_at=now(), notes=%s "
            "WHERE source=%s AND user_id=%s",
            (status, notes, source, user_id),
        )
    else:
        execute(
            "UPDATE sync_events SET status=%s, processed_at=now() "
            "WHERE source=%s AND user_id=%s",
            (status, source, user_id),
        )


def publish_only_yandex(users: List) -> dict:
    """Публикует пользователей «только в Яндексе» как события на архивацию.

    users — список YandexUser. Статус существующих событий НЕ сбрасывается
    (backed_up/deleted остаются), новые создаются со статусом 'new'.
    """
    if not users:
        return {"published": 0, "skipped": 0}
    published = 0
    skipped = 0
    for u in users:
        if not getattr(u, "id", None) and not getattr(u, "nickname", None):
            skipped += 1
            continue
        upsert_event(
            str(u.id or ""),
            u.nickname or "",
            email=u.email or None,
            display_name=u.display_name or None,
        )
        published += 1
    logger.info("Опубликовано %s событий на архивацию", published)
    return {"published": published, "skipped": skipped}


def close_returned_events(ad_logins: List[str], source: str = SOURCE) -> int:
    """Помечает события 'dismissed' для тех, кто снова появился в AD.

    Вызывается после сравнения: если человек снова есть в AD, он больше не
    кандидат на увольнение — событие закрывается.
    """
    logins = {x.strip().lower() for x in ad_logins if x and x.strip()}
    if not logins:
        return 0
    try:
        closed = execute(
            """
            UPDATE sync_events SET
                status='dismissed', processed_at=now(),
                notes='сотрудник снова в AD'
            WHERE source=%s
              AND status IN ('new','accepted','backed_up','delete_scheduled')
              AND lower(login) = ANY(%s)
            """,
            (source, list(logins)),
        )
        if closed:
            logger.info("Закрыто событий (возврат в AD): %s", closed)
        return closed
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось закрыть события: %s", e)
        return 0


# ------------------------------------------------------------
# Чтение событий
# ------------------------------------------------------------

def list_events(status: str | None = None, limit: int = 500) -> list[dict]:
    _ensure_table()
    if status:
        return query_all(
            "SELECT * FROM sync_events WHERE status=%s ORDER BY created_at DESC LIMIT %s",
            (status, limit),
        )
    return query_all(
        "SELECT * FROM sync_events ORDER BY created_at DESC LIMIT %s", (limit,)
    )


def get_event(user_id: str, source: str = SOURCE) -> dict | None:
    return query_one(
        "SELECT * FROM sync_events WHERE source=%s AND user_id=%s", (source, user_id)
    )


def event_status_for_login(login: str) -> str | None:
    """Актуальный статус по логину: live-JOIN с dismissals (всё в одной БД)."""
    row = query_one(
        """
        SELECT e.status AS event_status,
               d.backup_done_at, d.deletion_scheduled_at, d.deletion_done_at
        FROM sync_events e
        LEFT JOIN dismissals d ON d.user_id = e.user_id
        WHERE e.login ILIKE %s
        ORDER BY e.created_at DESC
        LIMIT 1
        """,
        (login,),
    )
    if not row:
        return None
    return _resolve_status(row)


def _resolve_status(row: dict) -> str:
    """Собирает итоговый статус из полей dismissal, если они есть."""
    if row.get("deletion_done_at"):
        return "deleted"
    if row.get("backup_done_at") and row.get("deletion_scheduled_at"):
        return "delete_scheduled"
    if row.get("backup_done_at"):
        return "backed_up"
    return row.get("event_status") or "new"


def fetch_statuses() -> dict[str, str]:
    """Возвращает {login_lower: status} для всех событий (live-JOIN с dismissals)."""
    try:
        rows = query_all(
            """
            SELECT e.login,
                   CASE
                       WHEN d.deletion_done_at IS NOT NULL THEN 'deleted'
                       WHEN d.backup_done_at IS NOT NULL AND d.deletion_scheduled_at IS NOT NULL THEN 'delete_scheduled'
                       WHEN d.backup_done_at IS NOT NULL THEN 'backed_up'
                       ELSE e.status
                   END AS status
            FROM sync_events e
            LEFT JOIN dismissals d ON d.user_id = e.user_id
            WHERE e.source = %s
            """,
            (SOURCE,),
        )
        return {row["login"].lower(): row["status"] for row in rows if row.get("login")}
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось получить статусы событий: %s", e)
        return {}


def fetch_events() -> List[dict]:
    """Все события интеграции (для расширенного просмотра/отладки)."""
    _ensure_table()
    return query_all("SELECT * FROM sync_events ORDER BY created_at DESC LIMIT 500")


# ------------------------------------------------------------
# Приём событий: создание dismissals
# ------------------------------------------------------------

def import_sync_events(org_id: str = "") -> dict:
    """Принимает новые события: создаёт dismissals, обновляет статусы.

    Возвращает статистику: {"imported": N, "skipped": N}.
    Идемпотентно: повторный запуск ничего не дублирует.
    """
    from core.config import get_settings

    org = org_id or get_settings().yandex_org_id

    events = list_events(status="new", limit=1000)
    imported = 0
    skipped = 0
    for ev in events:
        user_id = ev["user_id"]
        login = ev["login"]
        try:
            # Пользователь должен существовать в users из-за FK dismissals.user_id
            _ensure_user_row(user_id, login, org, ev.get("email"), ev.get("display_name"))
            created = dismissals.create_dismissal(
                user_id, login, org, notes="auto: only_in_yandex (нет пары в AD)"
            )
            if created:
                imported += 1
            else:
                skipped += 1
            # Статус события — по фактическому состоянию dismissal
            d = dismissals.get_dismissal(user_id)
            status = dismissals.dismissal_state(d) if d else "accepted"
            update_event_status(user_id, status, notes="принято в архивацию")
            logger.info("sync_events: %s -> %s (%s)", login, status, "imported" if created else "already_exists")
        except Exception:  # noqa: BLE001
            logger.exception("sync_events: не удалось импортировать %s", login)
            skipped += 1
    logger.info("import_sync_events: imported=%s, skipped=%s", imported, skipped)
    return {"imported": imported, "skipped": skipped}


def _ensure_user_row(
    user_id: str, login: str, org_id: str, email: str | None, display_name: str | None
) -> None:
    """Гарантирует наличие пользователя в users (FK для dismissals)."""
    execute(
        """
        INSERT INTO users (id, org_id, login, nickname, name, email, status)
        VALUES (%s,%s,%s,%s,%s,%s,'disabled')
        ON CONFLICT (id) DO UPDATE SET
            org_id = EXCLUDED.org_id,
            login = EXCLUDED.login,
            email = COALESCE(EXCLUDED.email, users.email),
            status = 'disabled'
        """,
        (user_id, org_id, login, login, display_name, email),
    )


# ------------------------------------------------------------
# Бэкфилл существующих dismissals (одноразовая миграция)
# ------------------------------------------------------------

def backfill_events() -> dict:
    """Записывает все существующие dismissals в sync_events (для целостной истории)."""
    rows = dismissals.list_dismissals(limit=100000)
    total = 0
    updated = 0
    for r in rows:
        total += 1
        status = dismissals.dismissal_state(r)
        upsert_event(
            r["user_id"],
            r["login"],
            email=r.get("email"),
            display_name=r.get("user_name"),
            status=status,
            notes="backfill: существующий dismissal",
        )
        if status != "new":
            updated += 1
    logger.info("backfill_events: total=%s, with_status=%s", total, updated)
    return {"total": total, "updated": updated}


# ------------------------------------------------------------
# Текстовые статусы для UI
# ------------------------------------------------------------

STATUS_LABELS = {
    "new": "Новый",
    "accepted": "Принят",
    "backed_up": "Бэкап сделан",
    "delete_scheduled": "Удаление запланировано",
    "deleted": "Ящик удалён",
    "dismissed": "Вернулся в AD",
    "detected": "Обнаружен",
}
