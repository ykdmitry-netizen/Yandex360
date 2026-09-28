"""Задачи снимков почтовых ящиков (в т.ч. уволенных сотрудников).

В объединённом проекте Celery/Redis не используются: scheduler и UI вызывают
эти функции напрямую (в фоновом потоке), поэтому каждая функция — обычная.
"""
from __future__ import annotations

from core.logs import get_logger
from integrations import mail_tokens as tokens
from services import directory, state
from services import dismissals as dismissal
from services.imap_backup import backup_user_mailbox
from services.storage import INDEX_FILENAME, get_storage

logger = get_logger(__name__)


def perform_user_backup(org_id: str, login: str, reason: str = "manual") -> dict:
    """Снимок одного ящика: token-exchange -> IMAP -> MBOX -> manifest + sha256 -> статус в БД.

    Снимок пишется в новый каталог (никогда не перезаписывает прошлые).
    """
    run_id = state.start_run(org_id, login, reason)
    storage = get_storage()

    def _log(msg: str) -> None:
        state.log(run_id, "info", msg)

    try:
        user = directory.find_user(login)
        if not user:
            raise ValueError(f"Пользователь {login} не найден в локальной БД")

        _log(f"Получение временного токена для {login}")
        mailbox_email = user.email or login
        access_token = tokens.exchange_token(login, mailbox_email)

        # 1. Новый уникальный каталог снимка
        snapshot_dir = storage.prepare_snapshot(org_id, login)
        _log(f"Каталог снимка: {snapshot_dir}")

        # 2. Выгрузка писем в MBOX (XOAUTH2 авторизуется по полному email)
        result = backup_user_mailbox(
            email=mailbox_email,
            access_token=access_token,
            snapshot_dir=snapshot_dir,
            log_func=_log,
        )

        # 3. Архив + sha256
        archive_path, archive_sha = storage.archive_snapshot(snapshot_dir)

        # 4. Manifest рядом со снимком
        storage.write_manifest(
            snapshot_dir,
            org_id=org_id,
            login=login,
            reason=reason,
            folders_count=result["folders_count"],
            messages=result["messages"],
            size_bytes=result["size_bytes"],
            archive_name=archive_path.name,
            archive_sha256=archive_sha,
            extra={
                "index": INDEX_FILENAME,
                "index_entries": result.get("index_entries", 0),
            },
        )

        # 5. Статус в БД
        state.finish_run(
            run_id,
            messages=result["messages"],
            size_bytes=result["size_bytes"],
            path=str(snapshot_dir),
            sha256=archive_sha,
        )

        # 6. Если это снимок уволенного — фиксируем в dismissals
        if reason in ("dismissal", "pre_delete"):
            dismissal.mark_backup_done(user.id, run_id)

        _log(f"Снимок завершён: {result['messages']} писем, sha256={archive_sha[:16]}…")
        return {
            **result,
            "sha256": archive_sha,
            "archive": str(archive_path),
            "snapshot_dir": str(snapshot_dir),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка снимка %s", login)
        detail = str(exc)
        if "AUTHENTICATIONFAILED" in detail:
            detail = (
                "Яндекс отказал в IMAP-доступе (AUTHENTICATIONFAILED): ящик заблокирован "
                "или IMAP отключён. Снимок возможен только пока ящик активен — "
                "делайте снимок ДО блокировки в Яндексе."
            )
        state.fail_run(run_id, detail)
        state.log(run_id, "error", detail)
        raise


def backup_all(org_id: str) -> dict:
    """Снимки всех пользователей (reason=scheduled), последовательно."""
    users = directory.list_users(org_id)
    ok = 0
    for u in users:
        try:
            perform_user_backup(org_id, u.login, "scheduled")
            ok += 1
        except Exception:  # noqa: BLE001
            logger.exception("Снимок %s не удался", u.login)
    logger.info("Снимки завершены: %s/%s", ok, len(users))
    return {"dispatched": ok, "total": len(users)}


def backup_dismissed(org_id: str) -> dict:
    """Снимки для всех уволенных, кому бэкап ещё не сделан."""
    pending = dismissal.find_not_backed_up()
    ok = 0
    for d in pending:
        try:
            perform_user_backup(org_id, d["login"], "dismissal")
            ok += 1
        except Exception:  # noqa: BLE001
            logger.exception("Снимок %s не удался", d["login"])
    return {"dispatched": ok, "total": len(pending)}
