"""Жизненный цикл уволенного сотрудника (dismissals).

Состояния:
  detected         -> сотрудник помечен как уволенный (вручную или из Directory)
  backed_up        -> снимок почты сделан
  delete_scheduled -> назначена дата удаления ящика (делает человек)
  deleted          -> ящик удалён в Яндексе
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.config import get_settings
from core.db import execute, query_all, query_one
from core.logs import get_logger
from services import state

logger = get_logger(__name__)

DISABLED_STATUSES = {"disabled", "fired", "deleted"}


# ---------- Запись ----------

def create_dismissal(
    user_id: str,
    login: str,
    org_id: str,
    fired_at: datetime | None = None,
    notes: str | None = None,
) -> int:
    """Создаёт запись об увольнении (если ещё нет). Возвращает число вставленных строк."""
    return execute(
        """
        INSERT INTO dismissals (user_id, login, org_id, fired_at, notes)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id, login, org_id, fired_at, notes),
    )


def mark_backup_done(user_id: str, run_id: int) -> None:
    """Фиксирует снимок и назначает срок удаления ящика по должности (ТЗ п.3).

    Срок берётся из реестра retention_rules; подобранное правило и должность
    запоминаются в строке увольнения, чтобы последующая перенастройка реестра
    не меняла уже назначенные даты.
    """
    from services import retention

    row = query_one(
        """
        SELECT u.position
        FROM dismissals d
        LEFT JOIN users u ON u.id = d.user_id
        WHERE d.user_id=%s
        """,
        (user_id,),
    )
    position = (row or {}).get("position")
    plan = retention.plan_deletion(position)
    rule = plan["rule"]

    retention_days = int(state.get_setting("retention_days", "365"))
    now = datetime.now(timezone.utc)
    retention_until = now + timedelta(days=retention_days)

    if rule is None:
        # Реестр правил пуст или все правила выключены — прежнее поведение:
        # один глобальный срок, чтобы ящик не был удален досрочно.
        delete_after_days = int(state.get_setting("delete_mailbox_after_days", "90"))
        deletion_scheduled = now + timedelta(days=delete_after_days)
        rule_name = None
        logger.warning(
            "Для %s правило хранения не подобрано, срок удаления по глобальной настройке: %s дней",
            user_id, delete_after_days,
        )
    else:
        deletion_scheduled = plan["deletion_at"]
        rule_name = rule.name
        logger.info(
            "Для %s применено правило «%s» (%s), удаление ящика планируется %s",
            user_id, rule.name, rule.label, deletion_scheduled.date(),
        )

    # --- Продакшн-логика до ТЗ п.3 (один глобальный срок для всех) ---
    # retention_days = int(state.get_setting("retention_days", "365"))
    # delete_after_days = int(state.get_setting("delete_mailbox_after_days", "90"))
    # now = datetime.now(timezone.utc)
    # retention_until = now + timedelta(days=retention_days)
    # deletion_scheduled = now + timedelta(days=delete_after_days)
    # ------------------------------------------------------------------

    execute(
        """
        UPDATE dismissals SET
            backup_done_at=now(),
            backup_run_id=%s,
            deletion_scheduled_at=%s,
            retention_until=%s,
            position=%s,
            retention_rule=%s
        WHERE user_id=%s
        """,
        (run_id, deletion_scheduled, retention_until, position, rule_name, user_id),
    )
    notes = f"бэкап сделан; правило хранения: {rule_name}" if rule_name else "бэкап сделан"
    _sync_event_status(user_id, "backed_up", notes=notes)


def reconcile_deleted(yandex_logins, ad_logins, log_func=None) -> int:
    """Отмечает карточки удалёнными, если логина нет ни в Яндексе, ни в AD.

    Сроки хранения в организации отрабатывает внешний скрипт: по наступлении
    срока он удаляет учётную запись в Яндекс 360. Консоль ничего не удаляет,
    но обязана заметить результат — иначе карточка навсегда останется в
    состоянии «удаление запланировано», а показатель «Удалено» не наполнится.
    """
    yandex = {str(x).strip().lower() for x in (yandex_logins or []) if str(x).strip()}
    ad = {str(x).strip().lower() for x in (ad_logins or []) if str(x).strip()}
    marked = 0
    for row in query_all("SELECT user_id, login, deletion_done_at FROM dismissals"):
        login = str(row.get("login") or "").strip().lower()
        if not login or row.get("deletion_done_at"):
            continue
        if login in yandex or login in ad:
            continue
        mark_deleted(row["user_id"])
        marked += 1
        if log_func:
            log_func(f"{row['login']}: ящика нет ни в Яндексе, ни в AD — отмечен удалённым")
    return marked


def mark_deleted(user_id: str) -> None:
    execute("UPDATE dismissals SET deletion_done_at=now() WHERE user_id=%s", (user_id,))
    _sync_event_status(user_id, "deleted", notes="ящик удалён в Яндексе")


def _sync_event_status(user_id: str, status: str, notes: str | None = None) -> None:
    """Передаёт статус в sync_events. Ленивый импорт — без цикла."""
    try:
        from services.integration import update_event_status

        update_event_status(user_id, status, notes=notes)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось обновить статус sync_events для %s", user_id)


def update_notes(user_id: str, notes: str) -> None:
    execute("UPDATE dismissals SET notes=%s WHERE user_id=%s", (notes, user_id))


# ---------- Чтение ----------

def list_dismissals(limit: int = 200) -> list[dict]:
    return query_all(
        """
        SELECT d.*, u.name AS user_name, u.email, u.status
        FROM dismissals d
        LEFT JOIN users u ON u.id = d.user_id
        ORDER BY COALESCE(d.fired_at, d.backup_done_at, now()) DESC
        LIMIT %s
        """,
        (limit,),
    )


def get_dismissal(user_id: str) -> dict | None:
    return query_one("SELECT * FROM dismissals WHERE user_id=%s", (user_id,))


def find_not_backed_up() -> list[dict]:
    """Уволенные, которым ещё не сделан бэкап."""
    return query_all(
        "SELECT * FROM dismissals WHERE backup_done_at IS NULL ORDER BY fired_at NULLS LAST"
    )


# ---------- Автообнаружение из Directory API ----------

def discover_from_directory(users) -> int:
    from services import filters

    """Проходит по пользователям и создаёт dismissals для тех, кто в disabled-статусе.

    users — список словарей из Directory API (с ключами id, nickname/email, status).
    Возвращает число новых обнаруженных уволенных.
    """
    cfg = get_settings()
    created = 0
    for u in users:
        status = (u.get("status") or "").lower()
        uid = u.get("id")
        if not uid or status not in DISABLED_STATUSES:
            continue
        login = u.get("nickname") or (u.get("email") or "").split("@")[0] or ""
        allowed, reason = filters.is_archivable(u)
        if not allowed:
            logger.info("Пропущен ящик %s: %s", login, reason)
            continue
        # уже есть в dismissals?
        if get_dismissal(uid):
            continue
        created += create_dismissal(uid, login, cfg.yandex_org_id, notes="auto: status={}".format(status))
        logger.info("Обнаружен уволенный: %s", login)
    return created


# ---------- Вспомогательное ----------

def dismissal_state(d: dict) -> str:
    """Возвращает состояние записи на основе полей."""
    if d.get("deletion_done_at"):
        return "deleted"
    if d.get("backup_done_at") and d.get("deletion_scheduled_at"):
        return "delete_scheduled"
    if d.get("backup_done_at"):
        return "backed_up"
    return "detected"
