"""Восстановление почты из снимка (MBOX).

Поддерживаются три гранулярности (ТЗ п.4):
  * весь ящик;
  * отдельная папка;
  * конкретные письма.
Результат — локальные файлы (.eml по одному или один .mbox для импорта в
почтовый клиент) либо, через services.imap_restore, отправка обратно в Яндекс.

Старые снимки (до появления index.jsonl) читаются разбиением MBOX по
разделителю "From "; папка в них не известна.
"""
from __future__ import annotations

import email
import json
import tarfile
from datetime import datetime
from pathlib import Path

from core.logs import get_logger
from services.storage import Storage, get_storage

logger = get_logger(__name__)

MBOX_FILENAME = "mail.mbox"
INDEX_FILENAME = "index.jsonl"
UNKNOWN_FOLDER = "Папки не записаны (снимок без индекса)"


class RestoreError(Exception):
    pass


# ---------- Поиск и проверка снимка ----------

def find_snapshot(storage: Storage, org_id: str, login: str, stamp: str | None = None) -> Path:
    """Возвращает каталог снимка (последний, если stamp не указан)."""
    backups = storage.list_backups(org_id, login)
    if not backups:
        raise RestoreError(f"Снимки для {login} не найдены")
    if stamp:
        target = storage.mailbox_dir(org_id, login, stamp)
        if target not in backups:
            raise RestoreError(f"Снимок {stamp} не найден")
        return target
    return backups[-1]


def verify_snapshot(storage: Storage, snapshot_dir: Path) -> bool:
    return storage.verify_snapshot(snapshot_dir)


def extract_to_dir(snapshot_dir: Path, target_dir: Path) -> Path:
    """Распаковывает .tar.gz снимка в target_dir, возвращает путь к MBOX."""
    archive = snapshot_dir.with_suffix(".tar.gz")
    if not archive.exists():
        raise RestoreError(f"Архив не найден: {archive}")
    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(target_dir, filter="data")
    mbox = target_dir / snapshot_dir.name / MBOX_FILENAME
    if not mbox.exists():
        raise RestoreError(f"В архиве нет {MBOX_FILENAME}")
    return mbox


def ensure_mbox(snapshot_dir: Path, work_dir: Path | None = None) -> Path:
    """Путь к mail.mbox: берётся из каталога снимка, при отсутствии — из архива."""
    mbox = snapshot_dir / MBOX_FILENAME
    if mbox.exists():
        return mbox
    if work_dir is None:
        raise RestoreError(f"Нет {MBOX_FILENAME} и некуда распаковать архив")
    return extract_to_dir(snapshot_dir, work_dir)


# ---------- Индекс снимка ----------

def read_index(snapshot_dir: Path, mbox_path: Path | None = None) -> list[dict]:
    """Записи индекса снимка: папка, UID, тема, смещение в mail.mbox.

    Для снимков без index.jsonl индекс строится обходом MBOX, поэтому
    восстановление всего ящика работает и на старых данных (но без группировки
    по папкам).
    """
    index_path = Path(snapshot_dir) / INDEX_FILENAME
    if index_path.exists():
        entries = [
            json.loads(line)
            for line in index_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if entries:
            return entries
    mbox = Path(mbox_path) if mbox_path else Path(snapshot_dir) / MBOX_FILENAME
    if not mbox.exists():
        raise RestoreError(f"Снимок не содержит ни {MBOX_FILENAME}, ни {INDEX_FILENAME}")
    return _index_from_mbox(mbox)


def _index_from_mbox(mbox_path: Path) -> list[dict]:
    """Совместимость со старыми снимками: разбор MBOX без готового индекса."""
    raw = mbox_path.read_bytes()
    starts: list[int] = []
    offset = 0
    for line in raw.split(b"\n"):
        if line.startswith(b"From "):
            starts.append(offset)
        offset += len(line) + 1
    entries: list[dict] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(raw)
        record = raw[start:end]
        msg = email.message_from_bytes(_record_to_message(record))
        entries.append({
            "folder": "",
            "folder_raw": "",
            "uid": "",
            "message_id": str(msg.get("Message-ID") or "").strip(),
            "subject": _decode_subject(msg),
            "date": str(msg.get("Date") or "").strip(),
            "from": _decode_header_text(msg.get("From")),
            "offset": start,
            "length": end - start,
        })
    return entries


def folder_summary(entries: list[dict]) -> list[dict]:
    """Папки снимка с числом писем — для дерева/фильтра в UI."""
    counts: dict[str, int] = {}
    for e in entries:
        key = e.get("folder") or UNKNOWN_FOLDER
        counts[key] = counts.get(key, 0) + 1
    return [{"folder": name, "messages": count} for name, count in sorted(counts.items())]


def select_messages(
    entries: list[dict],
    *,
    folders: list[str] | None = None,
    uids: list[str] | list[int] | None = None,
    message_ids: list[str] | None = None,
    keys: list[tuple[str, str]] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Фильтр индекса по папкам и/или конкретным письмам (UID, Message-ID).

    keys — пары (папка, UID): единственный способ указать письмо однозначно,
    потому что UID уникален только внутри папки.
    """
    selected = entries
    wanted_uids = {str(u) for u in uids} if uids else set()
    wanted_ids = {m.strip() for m in message_ids} if message_ids else set()
    wanted_keys = {(f or "", str(u)) for f, u in keys} if keys else set()
    if folders:
        norm = {f.strip().lower() for f in folders}
        selected = [e for e in selected if (e.get("folder") or "").strip().lower() in norm]
    if wanted_keys:
        selected = [
            e for e in selected
            if ((e.get("folder") or ""), str(e.get("uid") or "")) in wanted_keys
        ]
    if wanted_uids or wanted_ids:
        selected = [
            e for e in selected
            if str(e.get("uid") or "") in wanted_uids
            or str(e.get("message_id") or "").strip() in wanted_ids
        ]
    if limit is not None:
        selected = selected[:limit]
    return selected


# ---------- Чтение писем из MBOX ----------

def read_record(mbox_path: Path, entry: dict) -> bytes:
    """Запись MBOX целиком (с строкой-разделителем "From ...")."""
    mbox_path = Path(mbox_path)
    if not mbox_path.exists():
        raise RestoreError(f"Файл не найден: {mbox_path}")
    with mbox_path.open("rb") as f:
        f.seek(int(entry["offset"]))
        return f.read(int(entry["length"]))


def read_message(mbox_path: Path, entry: dict) -> bytes:
    """Чистое RFC822-письмо: без разделителя MBOX и с разэкранированным ">From "."""
    return _record_to_message(read_record(mbox_path, entry))


def _record_to_message(record: bytes) -> bytes:
    lines = record.split(b"\n")
    body = b"\n".join(lines[1:]) if lines and lines[0].startswith(b"From ") else record
    return _unescape(body).strip(b"\r\n") + b"\r\n"


# ---------- Экспорт ----------

def export_eml(messages: list[tuple[str, bytes]], out_dir: Path) -> list[Path]:
    """Пишет .eml файлы; messages — [(имя_файла, байты письма)]."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, payload in messages:
        path = out_dir / name
        path.write_bytes(payload)
        written.append(path)
    logger.info("Экспортировано .eml: %s в %s", len(written), out_dir)
    return written


def export_mbox(records: list[bytes], out_path: Path) -> Path:
    """Собирает один .mbox из выбранных записей (для импорта в почтовый клиент)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        for rec in records:
            if not rec.endswith(b"\n"):
                rec += b"\n"
            f.write(rec)
    logger.info("Экспортирован MBOX: %s писем в %s", len(records), out_path)
    return out_path


def eml_name(entry: dict, index: int) -> str:
    """Стабильное читаемое имя файла для письма из снимка."""
    subject = _safe_name(entry.get("subject") or "") or "no_subject"
    folder = _safe_name(entry.get("folder") or "")
    uid = _safe_name(str(entry.get("uid") or "")) or f"n{index}"
    prefix = f"{folder}_{uid}" if folder else f"{index:06d}"
    return f"{prefix}_{subject}.eml"


def _safe_name(text: str, max_len: int = 80) -> str:
    cleaned = "".join(c for c in text if c.isalnum() or c in "._- ").strip()
    return cleaned[:max_len]


def split_mbox_to_eml(mbox_path: Path, out_dir: Path) -> int:
    """Разбивает MBOX на отдельные .eml файлы. Возвращает число сообщений."""
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with mbox_path.open("rb") as f:
        raw = f.read()
    # Разделитель записей MBOX: строка, начинающаяся с "From " на границе
    records = _split_mbox(raw)
    for i, rec in enumerate(records, start=1):
        msg = email.message_from_bytes(rec)
        subject = _decode_subject(msg) or f"message_{i}"
        safe_subject = "".join(c for c in subject if c.isalnum() or c in "._- ")[:80].strip()
        fname = f"{i:06d}_{safe_subject or 'no_subject'}.eml"
        (out_dir / fname).write_bytes(rec)
        count += 1
    logger.info("Извлечено писем: %s в %s", count, out_dir)
    return count


def _split_mbox(raw: bytes) -> list[bytes]:
    """Делит MBOX на отдельные письма по разделителю From_.

    Строка-разделитель ("From MAILER-DAEMON ...") в письмо НЕ включается,
    а экранированные строки ">From " разворачиваются обратно в "From ".
    """
    lines = raw.split(b"\n")
    records: list[bytes] = []
    current: list[bytes] = []
    for line in lines:
        if line.startswith(b"From "):
            if current:
                records.append(_unescape(b"\n".join(current)))
            current = []
            continue
        current.append(line)
    if current:
        records.append(_unescape(b"\n".join(current)))
    return records


def _unescape(rec: bytes) -> bytes:
    """Разворачивает MBOX-экранирование ">From " -> "From "."""
    return b"\n".join(
        (line[1:] if line.startswith(b">From ") else line) for line in rec.split(b"\n")
    )


def _decode_subject(msg) -> str:
    """Разворачивает MIME-кодирование темы (=?utf-8?b?...?=) в читаемый текст."""
    return _decode_header_text(msg.get("Subject"))


def _decode_header_text(value) -> str:
    if not value:
        return ""
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:  # noqa: BLE001
        return str(value).strip()


# ---------- Высокоуровневые сценарии ----------

def restore_selection(
    org_id: str,
    login: str,
    out_root: Path,
    *,
    stamp: str | None = None,
    folders: list[str] | None = None,
    uids: list[str] | None = None,
    message_ids: list[str] | None = None,
    keys: list[tuple[str, str]] | None = None,
    as_mbox: bool = False,
) -> dict:
    """Восстановление выбранных писем в локальный каталог (ТЗ п.4, базовый режим).

    folders/uids/message_ids/keys не заданы — восстанавливается весь ящик.
    Возвращает {'snapshot','verified','messages','total_in_snapshot','partial',
    'folders','out_dir','eml_dir','mbox'}.
    """
    storage: Storage = get_storage()
    snap = find_snapshot(storage, org_id, login, stamp)
    return _restore_from_snapshot(
        snap, out_root,
        folders=folders, uids=uids, message_ids=message_ids, keys=keys, as_mbox=as_mbox,
    )


def restore_from_path(
    snapshot_dir: Path,
    out_root: Path,
    *,
    folders: list[str] | None = None,
    uids: list[str] | None = None,
    message_ids: list[str] | None = None,
    keys: list[tuple[str, str]] | None = None,
    as_mbox: bool = False,
) -> dict:
    """Восстановление по прямому пути к каталогу снимка (без поиска по БД)."""
    return _restore_from_snapshot(
        Path(snapshot_dir), out_root,
        folders=folders, uids=uids, message_ids=message_ids, keys=keys, as_mbox=as_mbox,
    )


def _restore_from_snapshot(
    snap: Path,
    out_root: Path,
    *,
    folders: list[str] | None,
    uids: list[str] | None,
    message_ids: list[str] | None,
    keys: list[tuple[str, str]] | None = None,
    as_mbox: bool,
) -> dict:
    storage = get_storage()
    verified = storage.verify_snapshot(snap)
    partial = bool(folders or uids or message_ids or keys)
    target = out_root / snap.parent.name / snap.name
    if partial:
        # Выборочное восстановление не должно смешиваться с предыдущими выгрузками
        target = target / f"выборка_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    target.mkdir(parents=True, exist_ok=True)
    # mail.mbox может лежать только в архиве — распаковываем во временный каталог
    mbox = ensure_mbox(snap, target / "_extract")
    entries = read_index(snap, mbox)
    selected = select_messages(
        entries, folders=folders, uids=uids, message_ids=message_ids, keys=keys
    )
    if not selected:
        raise RestoreError(
            "Ничего не выбрано для восстановления: проверьте папку, UID или Message-ID"
        )
    if uids and not folders and len(selected) > len(set(str(u) for u in uids)):
        logger.warning(
            "UID %s встречаются в нескольких папках — уточните folders, чтобы выбрать одно письмо",
            ",".join(str(u) for u in uids),
        )

    eml_dir = target / "eml"
    names = [
        (eml_name(e, i), read_message(mbox, e)) for i, e in enumerate(selected, start=1)
    ]
    export_eml(names, eml_dir)

    mbox_path = None
    if as_mbox or not partial:
        mbox_path = target / "restored.mbox"
        export_mbox([read_record(mbox, e) for e in selected], mbox_path)

    logger.info(
        "Из снимка %s восстановлено %s из %s писем -> %s",
        snap.name, len(selected), len(entries), target,
    )
    return {
        "snapshot": str(snap),
        "verified": verified,
        "messages": len(selected),
        "total_in_snapshot": len(entries),
        "partial": partial,
        "folders": folder_summary(selected),
        "out_dir": str(target),
        "eml_dir": str(eml_dir),
        "mbox": str(mbox_path) if mbox_path else None,
    }


def restore_mailbox(org_id: str, login: str, out_root: Path, stamp: str | None = None) -> dict:
    """Полный цикл восстановления всего ящика (без фильтра по папкам/письмам).

    Сохранён как отдельная функция: вызывающий код прода не должен меняться,
    когда добавляются выборочные сценарии.
    """
    return restore_selection(org_id, login, out_root, stamp=stamp)
