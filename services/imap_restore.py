"""Возврат писем из снимка в активный ящик Яндекс 360 (IMAP APPEND).

Это расширенное восстановление из ТЗ п.4 — то, что просят служба безопасности
и аудиторы. Возможны три адреса назначения:
  * ящик выбранного активного сотрудника;
  * исходный ящик (пока он ещё не удалён в Яндексе);
  * выделенный «ящик-хранилище» (RESTORE_STORAGE_MAILBOX).

Письма всегда кладутся в подпапку «Восстановлено/<login>/<папка из снимка>»,
чтобы возврат не смешивался с живой почтой.

Режим dry_run (по умолчанию) ничего не отправляет: он показывает план —
какие письма в какие папки попадут. Фактическая отправка выполняется только с
dry_run=False и фиксируется в backup_logs построчно.
"""
from __future__ import annotations

import imaplib
import tempfile
import time
from collections import OrderedDict
from datetime import timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional

from core.config import get_settings
from core.logs import get_logger
from integrations import mail_tokens as tokens
from services import directory, restore, state
from services.imap_backup import encode_imap_folder_name, imap_connect
from services.storage import get_storage

logger = get_logger(__name__)

APPEND_ROOT = "Восстановлено"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class ImapRestoreError(Exception):
    pass


# ---------- Адреса назначения ----------

def storage_mailbox() -> str:
    """Email «ящика-хранилища» для аудита: настройка БД или переменная окружения."""
    return (state.get_setting("restore_storage_mailbox", "")
            or get_settings().restore_storage_mailbox or "").strip()


def list_targets(org_id: str) -> list[dict]:
    """Куда можно вернуть почту: активные сотрудники + ящик-хранилище."""
    targets = [
        {
            "kind": "user",
            "login": u.login,
            "email": u.email or u.login,
            "label": f"{u.name or u.login} <{u.email or u.login}>",
        }
        for u in directory.list_users(org_id)
        if u.status == "active"
    ]
    storage_box = storage_mailbox()
    if storage_box:
        targets.append({
            "kind": "storage",
            "login": storage_box.split("@")[0],
            "email": storage_box,
            "label": f"Ящик-хранилище (аудит) <{storage_box}>",
        })
    return targets


def dest_folder(login: str, source_folder: str, root: str = APPEND_ROOT) -> str:
    """Имя папки назначения: Восстановлено/<login>/<папка из снимка>."""
    leaf = (source_folder or "").strip() or "Без папки"
    return f"{root.strip('/')}/{login}/{leaf}"


# ---------- Служебные ----------

def _internaldate(date_str: str | None) -> Optional[str]:
    """RFC2822-дата письма в формат IMAP INTERNALDATE: "01-Oct-2025 08:53:20 +0000"."""
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(str(date_str).strip())
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    delta = dt.utcoffset() or timedelta(0)
    total_minutes = int(delta.total_seconds() // 60)
    sign = "-" if total_minutes < 0 else "+"
    total_minutes = abs(total_minutes)
    tz_offset = f"{sign}{total_minutes // 60:02d}{total_minutes % 60:02d}"
    return (
        f'"{dt.day:02d}-{MONTHS[dt.month - 1]}-{dt.year:04d} '
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} {tz_offset}\""
    )


def _mailbox_ref(name: str) -> str:
    """Закавыченное имя папки для команд IMAP."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def ensure_folder(conn, encoded_name: str) -> bool:
    """Создаёт папку (и родительские сегменты), если её нет."""
    parts = encoded_name.split("/")
    for i in range(1, len(parts) + 1):
        leaf = "/".join(parts[:i])
        typ, _ = conn.select(_mailbox_ref(leaf))
        if typ == "OK":
            continue
        typ, data = conn.create(_mailbox_ref(leaf))
        if typ != "OK":
            # уже существует (гонка) или прав не хватает — проверим select
            if conn.select(_mailbox_ref(leaf))[0] == "OK":
                continue
            logger.warning("Не удалось создать папку %s: %s", leaf, data)
            return False
    return True


class _Writer:
    """Одно IMAP-соединение для пакетной отправки: готовит папки и пишет письма.

    Устойчивость к обрывам: при ошибке соединения переподключается и повторяет
    письмо (до 3 попыток). Prepared-папки запоминаются, чтобы не делать CREATE/SELECT
    перед каждым APPEND на ящике в сотни писем.
    """

    attempts = 3

    def __init__(self, target_email: str, access_token: str):
        self.target_email = target_email
        self.access_token = access_token
        self.conn = imap_connect(target_email, access_token)
        self._prepared: set[str] = set()

    def _reconnect(self) -> None:
        try:
            self.conn.logout()
        except Exception:  # noqa: BLE001
            pass
        self._prepared.clear()
        self.conn = imap_connect(self.target_email, self.access_token)

    def close(self) -> None:
        try:
            self.conn.logout()
        except Exception:  # noqa: BLE001
            pass

    def _ensure_selected(self, encoded_folder: str) -> None:
        if encoded_folder in self._prepared:
            return
        if not ensure_folder(self.conn, encoded_folder):
            raise ImapRestoreError(f"нет доступа к папке {encoded_folder}")
        self.conn.select(_mailbox_ref(encoded_folder))
        self._prepared.add(encoded_folder)

    @staticmethod
    def _already_there(conn, message_id: str) -> bool:
        """Есть ли такое письмо в выбранной папке (по заголовку Message-ID)."""
        bare = message_id.strip("<> ")
        typ, data = conn.uid("search", None, "HEADER", "Message-ID", bare)
        if typ != "OK" or not data or not data[0]:
            return False
        return bool(data[0].split())

    def append(self, encoded_folder: str, raw: bytes, internal_date: str | None,
               message_id: str = "") -> tuple[str, str | None]:
        """Пишет письмо. Возвращает ('ok'|'skipped'|'error', подробности)."""
        last_error = "неизвестная ошибка IMAP"
        for attempt in range(1, self.attempts + 1):
            try:
                self._ensure_selected(encoded_folder)
                if message_id and self._already_there(self.conn, message_id):
                    return "skipped", None
                typ, data = self.conn.append(
                    _mailbox_ref(encoded_folder), raw, None, internal_date
                )
                if typ == "OK":
                    return "ok", None
                last_error = str(data)
            except (imaplib.IMAP4.error, OSError, ImapRestoreError) as exc:
                last_error = str(exc)
                logger.warning("APPEND в %s: попытка %s/%s сорвалась (%s)",
                               encoded_folder, attempt, self.attempts, exc)
                time.sleep(2 * attempt)
                self._reconnect()
        return "error", last_error


# ---------- Основной сценарий ----------

def build_plan(
    org_id: str,
    login: str,
    *,
    stamp: str | None = None,
    folders: list[str] | None = None,
    uids: list[str] | None = None,
    message_ids: list[str] | None = None,
    keys: list[tuple[str, str]] | None = None,
    dest_root: str = APPEND_ROOT,
    snapshot_dir: Path | None = None,
) -> dict:
    """Список писем из снимка с указанием папки назначения (без отправки)."""
    storage = get_storage()
    snap = Path(snapshot_dir) if snapshot_dir else restore.find_snapshot(storage, org_id, login, stamp)
    work_dir = Path(tempfile.gettempdir()) / "yandex-restore-extract"
    mbox = restore.ensure_mbox(snap, work_dir)
    entries = restore.read_index(snap, mbox)
    selected = restore.select_messages(
        entries, folders=folders, uids=uids, message_ids=message_ids, keys=keys
    )
    if not selected:
        raise ImapRestoreError(
            "Ничего не выбрано для отправки: проверьте папку, UID или Message-ID"
        )
    plan = [
        {
            "entry": e,
            "source_folder": e.get("folder") or "",
            "dest": dest_folder(login, e.get("folder") or "", dest_root),
            # Яндекс принимает не-ASCII имена папок только в modified UTF-7
            "dest_encoded": encode_imap_folder_name(dest_folder(login, e.get("folder") or "", dest_root)),
        }
        for e in selected
    ]
    return {"snapshot": snap, "mbox": mbox, "total_in_snapshot": len(entries), "plan": plan}


def restore_to_mailbox(
    org_id: str,
    login: str,
    target_email: str,
    *,
    stamp: str | None = None,
    folders: list[str] | None = None,
    uids: list[str] | None = None,
    message_ids: list[str] | None = None,
    keys: list[tuple[str, str]] | None = None,
    dest_root: str = APPEND_ROOT,
    dry_run: bool = True,
    log_func: Callable[[str], None] | None = None,
) -> dict:
    """Возврат выбранных писем в ящик target_email через IMAP APPEND.

    dry_run=True (по умолчанию) — только план, соединение с Яндексом не открывается.
    Письма, которые уже есть в папке назначения (совпадает Message-ID), не
    дублируются: они идут в счётчик skipped.
    Возвращает {'dry_run','target','planned','appended','skipped','failed',
    'folders','snapshot','run_id'}.
    """
    if not target_email:
        raise ImapRestoreError("Не указан ящик-получатель")

    def _log(msg: str, level: str = "info") -> None:
        logger.info("Восстановление %s -> %s: %s", login, target_email, msg)
        state.log(run_id, level, msg)
        if log_func:
            log_func(msg)

    run_id = state.start_run(org_id, login, "restore_preview" if dry_run else "restore_mail")
    try:
        return _do_restore(
            run_id, org_id, login, target_email, stamp=stamp, folders=folders,
            uids=uids, message_ids=message_ids, keys=keys, dest_root=dest_root,
            dry_run=dry_run, _log=_log,
        )
    except Exception as exc:  # noqa: BLE001
        # запуск не должен оставаться в статусе 'running', иначе журнал врёт
        state.fail_run(run_id, str(exc))
        raise


def _do_restore(
    run_id: int,
    org_id: str,
    login: str,
    target_email: str,
    *,
    stamp: str | None,
    folders: list[str] | None,
    uids: list[str] | None,
    message_ids: list[str] | None,
    keys: list[tuple[str, str]] | None,
    dest_root: str,
    dry_run: bool,
    _log: Callable[..., None],
) -> dict:
    built = build_plan(
        org_id, login, stamp=stamp, folders=folders, uids=uids,
        message_ids=message_ids, keys=keys, dest_root=dest_root,
    )
    plan = built["plan"]
    snap = built["snapshot"]

    by_folder: "OrderedDict[str, list[dict]]" = OrderedDict()
    for item in plan:
        by_folder.setdefault(item["dest"], []).append(item)

    _log(
        f"План: писем — {len(plan)} из снимка {snap.name}, папок в ящике "
        f"{target_email} — {len(by_folder)} "
        f"(режим: {'предпросмотр' if dry_run else 'запись'})"
    )
    for folder_name, items in by_folder.items():
        _log(f"  -> «{folder_name}»: {len(items)} писем")

    if dry_run:
        state.finish_run(run_id, messages=0, size_bytes=0, path=str(snap))
        return {
            "run_id": run_id,
            "dry_run": True,
            "target": target_email,
            "snapshot": str(snap),
            "planned": len(plan),
            "appended": 0,
            "failed": 0,
            "folders": [{"folder": f, "messages": len(v)} for f, v in by_folder.items()],
        }

    # Боевая отправка: токен выпускается на адрес получателя
    _log(f"Выпуск временного токена для {target_email}")
    access_token = tokens.exchange_token(target_email.split("@")[0], target_email)
    mbox = built["mbox"]

    appended = skipped = failed = 0
    size_bytes = 0
    failures: list[str] = []
    writer = _Writer(target_email, access_token)
    try:
        for item in plan:
            entry = item["entry"]
            raw = restore.read_message(mbox, entry)
            status, error = writer.append(
                item["dest_encoded"], raw, _internaldate(entry.get("date")),
                str(entry.get("message_id") or ""),
            )
            tag = f"«{item['dest']}» uid={entry.get('uid')} {(entry.get('subject') or '')[:60]}"
            if status == "ok":
                appended += 1
                size_bytes += len(raw)
                _log(f"APPEND OK {tag}")
            elif status == "skipped":
                skipped += 1
                _log(f"Пропущено (такое письмо уже есть в ящике): {tag}")
            else:
                failed += 1
                failures.append(f"{item['dest']}/{entry.get('uid')}: {error}")
                _log(f"APPEND НЕ УДАЛСЯ {tag}: {error}", "error")
    finally:
        writer.close()

    _log(f"Итого: принято {appended}, уже было {skipped}, ошибок {failed}")
    if appended == 0 and skipped == 0:
        state.fail_run(run_id, "; ".join(failures)[:2000] or "ни одно письмо не принято")
        raise ImapRestoreError(f"Не удалось восстановить письма: {failures[:3]}")
    state.finish_run(run_id, messages=appended, size_bytes=size_bytes, path=str(snap))
    return {
        "run_id": run_id,
        "dry_run": False,
        "target": target_email,
        "snapshot": str(snap),
        "planned": len(plan),
        "appended": appended,
        "skipped": skipped,
        "failed": failed,
        "failures": failures,
        "folders": [{"folder": f, "messages": len(v)} for f, v in by_folder.items()],
    }
