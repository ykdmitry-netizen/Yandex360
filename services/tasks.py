"""Фоновые задачи: выполняются в локальных потоках процесса (без Celery/Redis).

UI и планировщик вызывают enqueue_* — задача уходит в daemon-поток, а прогресс
и статусы пишутся в PostgreSQL (backup_runs/backup_logs), поэтому страницы
«Задачи» и «Журнал» показывают всё как обычно.
"""
from __future__ import annotations

import threading
import time

from core.config import get_settings
from core.logs import get_logger
from services import state
from services.storage import get_storage

logger = get_logger(__name__)


def _run_in_thread(func, *args) -> str:
    """Выполняет задачу в локальном фоновом потоке. Возвращает id задачи."""
    task_id = f"{getattr(func, 'name', func.__name__)}-local-{int(time.time() * 1000)}"

    def _target() -> None:
        try:
            func(*args)
            logger.info("Фоновая задача завершена: %s", task_id)
        except Exception:  # noqa: BLE001
            logger.exception("Фоновая задача упала: %s", task_id)

    threading.Thread(target=_target, name=task_id[:60], daemon=True).start()
    logger.info("Запущена фоновая задача: %s", task_id)
    return task_id


# ---------- Периодические сценарии ----------

def discover_disabled() -> dict:
    """Синхронизирует пользователей и создаёт dismissals для disabled-статусов."""
    from integrations.yandex import yandex_client_from_settings
    from services import directory, dismissals

    users_count = directory.sync_users()
    # Пользователи из Directory API (не из локальной БД), чтобы видеть актуальный статус
    client = yandex_client_from_settings()
    raw_users = client.list_users()
    created = dismissals.discover_from_directory(raw_users)
    logger.info("discover_disabled: users=%s, new_dismissals=%s", users_count, created)
    return {"synced_users": users_count, "new_dismissals": created}


def import_events() -> dict:
    """Принимает кандидатов на увольнение (sync_events) в жизненный цикл архивации.

    Читает события со статусом 'new', создаёт dismissals и переводит события
    в актуальные статусы (accepted/backed_up/delete_scheduled/deleted).
    Идемпотентно: повторный запуск безопасен.
    """
    from services import integration

    result = integration.import_sync_events()
    logger.info("import_events: %s", result)
    return result


def backfill_events() -> dict:
    """Одноразовая миграция: существующие dismissals -> sync_events."""
    from services import integration

    result = integration.backfill_events()
    logger.info("backfill_events: %s", result)
    return result


def cleanup_dismissed() -> dict:
    """Очистка: удаление снимков старше retention_days."""
    retention_days = int(state.get_setting("retention_days", "365"))
    removed = get_storage().cleanup_older_than(retention_days)
    logger.info("cleanup_dismissed: removed_snapshots=%s", removed)
    return {"removed_snapshots": removed, "retention_days": retention_days}


def process_pending() -> dict:
    """Снимки для всех уволенных без бэкапа."""
    from services import dismissals as dismissal
    from services.backup import perform_user_backup

    cfg = get_settings()
    pending = dismissal.find_not_backed_up()
    ok = 0
    for d in pending:
        try:
            perform_user_backup(cfg.yandex_org_id, d["login"], "dismissal")
            ok += 1
        except Exception:  # noqa: BLE001
            logger.exception("Снимок %s не удался", d["login"])
    logger.info("process_pending: done=%s/%s", ok, len(pending))
    return {"dispatched": ok, "total": len(pending)}


# ---------- Постановка в фоновый поток ----------

def enqueue_user_backup(org_id: str, login: str, reason: str = "manual") -> str:
    """Снимок одного ящика. Возвращает id задачи."""
    from services.backup import perform_user_backup

    return _run_in_thread(perform_user_backup, org_id, login, reason)


def enqueue_all_backup(org_id: str) -> str:
    """Снимки всех пользователей (reason=scheduled)."""
    from services.backup import backup_all

    return _run_in_thread(backup_all, org_id)


def enqueue_dismissal_backup(org_id: str, login: str) -> str:
    """Снимок уволенного сотрудника (reason=dismissal)."""
    return enqueue_user_backup(org_id, login, "dismissal")


def enqueue_discover_disabled() -> str:
    """Автообнаружение уволенных (disabled-статусы в Directory API)."""
    return _run_in_thread(discover_disabled)


def enqueue_import_events() -> str:
    """Импорт кандидатов на увольнение из sync_events."""
    return _run_in_thread(import_events)


def enqueue_backfill_events() -> str:
    """Одноразовая миграция: существующие dismissals -> sync_events."""
    return _run_in_thread(backfill_events)


def enqueue_process_pending() -> str:
    """Снимки для всех уволенных без бэкапа."""
    return _run_in_thread(process_pending)


def enqueue_cleanup() -> str:
    """Очистка старых снимков."""
    return _run_in_thread(cleanup_dismissed)
