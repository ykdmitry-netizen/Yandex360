"""Бэкап ящика сотрудника по IMAP (XOAUTH2) в MBOX + индекс писем.

Сценарий «снимок уволенного»: за один запуск делается полный снимок ящика
в новый каталог (никогда не перезаписывается).
"""
from __future__ import annotations

import base64
import email
import imaplib
import json
import re
import time
from pathlib import Path
from typing import Iterator

from core.config import get_settings
from core.logs import get_logger
from services.storage import INDEX_FILENAME

logger = get_logger(__name__)


class ImapBackupError(Exception):
    pass


def _xoauth2_string(email: str, access_token: str) -> bytes:
    """Формирует строку SASL XOAUTH2 для IMAP (user — полный email)."""
    auth = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
    return auth.encode("utf-8")


def imap_connect(email: str, access_token: str):
    cfg = get_settings()
    if cfg.imap_host.strip().lower() in ("mock", "mock-imap"):
        from services.imap_mock import MockIMAP4
        return MockIMAP4(email)
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=120)
    conn.authenticate("XOAUTH2", lambda _: _xoauth2_string(email, access_token))
    return conn


# Формат ответа LIST: "* LIST (флаги) "разделитель" "имя"" — префикс "* LIST "
# обязателен, имя может содержать пробелы и модифицированный UTF-7.
_IMAP_LIST_RE = re.compile(r'^.*?\([^)]*\)\s+("[^"]*"|\S+)\s+(.+)$')


def _parse_list_folder_name(line: str) -> str:
    """Извлекает имя папки из строки ответа LIST (с пробелами и экранированием)."""
    match = _IMAP_LIST_RE.match(line)
    if not match:
        parts = line.split()
        return parts[-1] if parts else ""
    raw = match.group(2).strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1].replace(r'\"', '"').replace(r"\\", "\\")
    return raw


def _decode_imap_folder_name(folder_name: str) -> str:
    """Декодирует modified UTF-7 имя папки в человекочитаемый вид."""
    if folder_name.upper() == "INBOX" or "&" not in folder_name:
        return folder_name
    result: list[str] = []
    index = 0
    while index < len(folder_name):
        if folder_name[index] != "&":
            result.append(folder_name[index])
            index += 1
            continue
        end = folder_name.find("-", index)
        if end == -1:
            end = len(folder_name)
        if end == index + 1:
            result.append("&")
            index = end + 1
            continue
        encoded = folder_name[index + 1:end].replace(",", "/")
        padding = (4 - len(encoded) % 4) % 4
        encoded += "=" * padding
        try:
            decoded_bytes = base64.b64decode(encoded)
            result.append(decoded_bytes.decode("utf-16-be"))
        except Exception:  # noqa: BLE001
            result.append(folder_name[index:end + 1])
        index = end + 1
    return "".join(result)


def encode_imap_folder_name(folder_name: str) -> str:
    """Кодирует имя папки в modified UTF-7 (требование IMAP для не-ASCII имён).

    Обратная операция к _decode_imap_folder_name: нужна при записи писем обратно,
    чтобы папка «Отправленные» создалась с тем же именем, что было в снимке.
    """
    if folder_name.isascii():
        return folder_name.replace("&", "&-")
    parts: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        b64 = base64.b64encode("".join(run).encode("utf-16-be")).decode("ascii")
        parts.append("&" + b64.rstrip("=").replace("/", ",") + "-")
        run.clear()

    for char in folder_name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            parts.append("&-" if char == "&" else char)
        else:
            run.append(char)
    flush()
    return "".join(parts)


def list_folders(conn) -> list[str]:
    """Возвращает имена папок из открытого соединения (без лишнего подключения)."""
    status, data = conn.list()
    if status != "OK":
        return []
    folders: list[str] = []
    for item in data:
        if not item:
            continue
        if isinstance(item, tuple):
            item = item[0]
        decoded = item.decode("utf-8", errors="replace")
        name = _parse_list_folder_name(decoded)
        if name:
            folders.append(name)
    return folders


def _select_ok(conn, folder: str) -> bool:
    """EXAMINE папки; имена с пробелами/спецсимволами требуют кавычек.

    Сначала пробуем закавыченное экранированное имя, затем как есть.
    """
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    for mailbox_ref in (f'"{escaped}"', folder):
        typ, _ = conn.select(mailbox_ref, readonly=True)
        if typ == "OK":
            return True
    return False


def _iter_messages(conn, folder: str) -> Iterator[tuple[str, bytes]]:
    """Возвращает (UID, сырые байты) каждого письма из папки.

    Батчи по 25 писем + периодический NOOP, чтобы Яндекс не рвал соединение
    на длинных операциях. UID нужен для индекса снимка (точечное восстановление).
    """
    if not _select_ok(conn, folder):
        logger.warning("Не удалось открыть папку %s", folder)
        return
    typ, data = conn.uid("search", None, "ALL")
    if typ != "OK":
        return
    uids = data[0].split()
    batch_size = 25
    fetched_total = 0
    for i in range(0, len(uids), batch_size):
        if fetched_total and fetched_total % 250 == 0:
            try:
                conn.noop()
            except Exception:  # noqa: BLE001
                pass
        chunk = b",".join(uids[i:i + batch_size])
        typ, fetched = conn.uid("fetch", chunk, "(UID RFC822)")
        if typ != "OK":
            continue
        for resp in fetched:
            if isinstance(resp, tuple) and len(resp) >= 2:
                fetched_total += 1
                yield _fetch_uid(resp[0]), resp[1]


# Ответ FETCH выглядит как b"12 (UID 47 RFC822 {292}" — UID берём из префикса.
_FETCH_UID_RE = re.compile(r"UID\s+(\d+)", re.IGNORECASE)


def _fetch_uid(prefix) -> str:
    if isinstance(prefix, bytes):
        prefix = prefix.decode("utf-8", errors="replace")
    match = _FETCH_UID_RE.search(str(prefix))
    return match.group(1) if match else ""


def backup_user_mailbox(email: str, access_token: str, snapshot_dir: Path, log_func=None) -> dict:
    """Полный бэкап ящика в snapshot_dir/mail.mbox (XOAUTH2 по email).

    Устойчивость к обрывам IMAP («socket error: EOF», «problems with connection»):
    список папок читается отдельным соединением, а каждая папка — своим свежим
    соединением с ретраями. При сбое частичная запись папки откатывается
    (seek+truncate) и папка читается заново — дубли в MBOX не появляются.

    Возвращает {'folders_count', 'messages', 'size_bytes', 'index_entries'}.

    Рядом с MBOX пишется index.jsonl — по одной JSON-строке на письмо
    (папка, UID, Message-ID, тема, дата, offset/length в mail.mbox). Индекс
    нужен для восстановления отдельной папки или одного письма; байты самих
    писем при этом не изменяются, поэтому контрольная сумма снимка остаётся
    корректной и для старых снимков без индекса.
    """
    def _log(msg: str) -> None:
        logger.info(msg)
        if log_func:
            log_func(msg)

    mbox_path = snapshot_dir / "mail.mbox"

    # 1. Список папок — отдельным соединением
    conn0 = imap_connect(email, access_token)
    try:
        folders = list_folders(conn0)
    finally:
        try:
            conn0.logout()
        except Exception:  # noqa: BLE001
            pass
    _log(f"Папок в ящике: {len(folders)}")

    # 2. Каждая папка — своё соединение + до 3 попыток
    total_messages = 0
    index_entries: list[dict] = []
    with mbox_path.open("wb") as f:
        for folder in folders:
            messages = 0
            folder_index: list[dict] = []
            last_error: Exception | None = None
            for attempt in range(1, 4):
                start = f.tell()
                conn = imap_connect(email, access_token)
                try:
                    count = 0
                    folder_index = []
                    for uid, raw in _iter_messages(conn, folder):
                        offset = f.tell()
                        record = _mbox_record(raw)
                        f.write(record)
                        count += 1
                        folder_index.append(
                            _index_entry(raw, folder, uid, offset, len(record))
                        )
                    messages = count
                    last_error = None
                    break
                except (imaplib.IMAP4.error, OSError) as exc:
                    f.seek(start)
                    f.truncate()
                    last_error = exc
                    logger.warning(
                        "Папка %s: попытка %s/3 не удалась (%s)", folder, attempt, exc
                    )
                    time.sleep(2 * attempt)
                finally:
                    try:
                        conn.logout()
                    except Exception:  # noqa: BLE001
                        pass
            if last_error is not None:
                raise ImapBackupError(
                    f"Папка «{_decode_imap_folder_name(folder)}»: не удалось прочитать "
                    f"после 3 попыток: {last_error}"
                ) from last_error
            index_entries.extend(folder_index)
            total_messages += messages
            _log(f"Папка «{_decode_imap_folder_name(folder)}»: {messages} писем")

    # 3. Индекс писем (папка + смещение в MBOX) — для точечного восстановления
    index_path = snapshot_dir / INDEX_FILENAME
    with index_path.open("w", encoding="utf-8") as idx:
        for entry in index_entries:
            idx.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _log(f"Индекс снимка: {len(index_entries)} писем в {index_path.name}")

    total_size = mbox_path.stat().st_size
    return {
        "folders_count": len(folders),
        "messages": total_messages,
        "size_bytes": total_size,
        "index_entries": len(index_entries),
        "path": str(mbox_path),
    }


def _index_entry(raw: bytes, folder: str, uid: str, offset: int, length: int) -> dict:
    """Строка индекса снимка: где в MBOX лежит это письмо и из какой оно папки."""
    msg = email.message_from_bytes(raw)
    return {
        "folder": _decode_imap_folder_name(folder),
        "folder_raw": folder,
        "uid": str(uid or ""),
        "message_id": str(msg.get("Message-ID") or "").strip(),
        "subject": _decode_header_text(msg.get("Subject")),
        "date": str(msg.get("Date") or "").strip(),
        "from": _decode_header_text(msg.get("From")),
        "offset": offset,
        "length": length,
    }


def _decode_header_text(value) -> str:
    """Читаемое значение заголовка (MIME-кодирование развёрнуто)."""
    if not value:
        return ""
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:  # noqa: BLE001
        return str(value).strip()


def _mbox_record(raw: bytes) -> bytes:
    """Преобразует RFC822-письмо в запись MBOX (байтово, без порчи вложений).

    MBOX — байтовый формат: тело письма копируется как есть, экранируются
    только строки, начинающиеся с "From " (кроме первой). Никаких декодирований
    в str — иначе бинарные вложения (PDF/DOCX/картинки) испортятся.
    """
    msg = email.message_from_bytes(raw)
    date_str = str(msg.get("Date") or "").strip().replace("\r", "").replace("\n", "")
    header = f"From MAILER-DAEMON {date_str}\n".encode("ascii", "replace")

    body_lines = raw.split(b"\n")
    escaped = [body_lines[0]] + [
        (b">" + line if line.startswith(b"From ") else line)
        for line in body_lines[1:]
    ]
    body = b"\n".join(escaped)
    if not body.endswith(b"\n"):
        body += b"\n"
    return header + body
