#!/usr/bin/env python3
"""Достройка снимка ящика из уже выгруженного mail.mbox.

Когда снимок прерывался, каталог оставался с одним mail.mbox: без
manifest.json и .tar.gz он не считается валидным и не виден в консоли как
готовый. Скрипт достраивает индекс писем (index.jsonl), манифест и архив,
НЕ обращаясь к почтовому ящику (IMAP не используется).

В манифест добавляются признаки восстановления, чтобы такие снимки нельзя
было спутать со штатными:
    "rebuilt_from_mbox": true,
    "rebuilt_note": "снимок достроен из mail.mbox; повторное чтение ящика не выполнялось"

Безопасность: если mbox не заканчивается переводом строки (признак обрыва),
скрипт отказывается достраивать снимок — нужен повторный снимок с ящика.

Запуск:
    venv/bin/python scripts/finalize_snapshot.py --list
    venv/bin/python scripts/finalize_snapshot.py --login zverev.e --dry-run
    venv/bin/python scripts/finalize_snapshot.py --login zverev.e
    venv/bin/python scripts/finalize_snapshot.py --all-missing
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from email import message_from_bytes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.restore import _decode_header_text, _decode_subject, _record_to_message  # noqa: E402
from services.storage import INDEX_FILENAME, get_storage  # noqa: E402

HEADER_SCAN_BYTES = 8192


def incomplete_dirs(org_id: str) -> dict[str, list[Path]]:
    """Каталоги-снимки без архива или без манифеста, сгруппированные по логину."""
    storage = get_storage()
    result: dict[str, list[Path]] = {}
    root = storage.root / org_id
    if not root.exists():
        return result
    for login_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for snap in sorted(p for p in login_dir.iterdir() if p.is_dir()):
            if snap.with_suffix(".tar.gz").exists() and (snap / "manifest.json").exists():
                continue
            result.setdefault(login_dir.name, []).append(snap)
    return result


def scan_mbox(mbox: Path) -> dict:
    """Потоковый обход MBOX: индексация писем без загрузки файла в память."""
    entries: list[dict] = []
    starts: list[int] = []
    offset = 0
    size = mbox.stat().st_size
    with mbox.open("rb") as fh:
        while True:
            line = fh.readline()
            if not line:
                break
            if line.startswith(b"From "):
                starts.append(offset)
            offset += len(line)
    total = offset

    with mbox.open("rb") as fh:
        for n, start in enumerate(starts):
            end = starts[n + 1] if n + 1 < len(starts) else total
            fh.seek(start)
            head = fh.read(min(HEADER_SCAN_BYTES, end - start))
            message = message_from_bytes(_record_to_message(head))
            entries.append({
                "folder": "",
                "folder_raw": "",
                "uid": "",
                "message_id": str(message.get("Message-ID") or "").strip(),
                "subject": _decode_subject(message),
                "date": str(message.get("Date") or "").strip(),
                "from": _decode_header_text(message.get("From")),
                "offset": start,
                "length": end - start,
            })

    tail = b""
    with mbox.open("rb") as fh:
        fh.seek(max(0, size - 2))
        tail = fh.read()
    return {
        "messages": len(entries),
        "entries": entries,
        "size": size,
        "ends_with_newline": tail.endswith(b"\n"),
    }


def expected_snapshot_size(login: str) -> int | None:
    """Размер успешного снимка этого логина по данным БД (или None)."""
    try:
        from core.db import query_one

        row = query_one(
            "SELECT max(size_bytes) AS size FROM backup_runs "
            "WHERE login=%s AND status='success' AND size_bytes > 0", (login,))
        return int(row["size"]) if row and row.get("size") else None
    except Exception:  # noqa: BLE001 — без БД просто не сверяем размеры
        return None


def finalize(snapshot_dir: Path, dry_run: bool, force: bool = False) -> dict:
    storage = get_storage()
    login = snapshot_dir.parent.name
    org_id = snapshot_dir.parent.parent.name
    mbox = snapshot_dir / "mail.mbox"
    if not mbox.exists():
        return {"ok": False, "reason": f"нет {mbox.name}"}

    scan = scan_mbox(mbox)
    if not scan["ends_with_newline"] and not force:
        return {"ok": False, "reason": "mbox обрывается без перевода строки — нужен повторный снимок ящика",
                "messages": scan["messages"], "size": scan["size"]}
    if scan["messages"] == 0:
        return {"ok": False, "reason": "в mbox не найдено ни одного письма", "size": scan["size"]}

    # Если в БД зафиксирован успешный снимок заметно большего размера, значит
    # mbox оборван на середине: достраивать нельзя, нужен повторный снимок ящика.
    expected = expected_snapshot_size(login)
    if expected and scan["size"] < expected * 0.9:
        return {"ok": False,
                "reason": (f"в БД зафиксирован снимок {expected / 2**30:.1f} ГБ, "
                           f"а в mbox всего {scan['size'] / 2**30:.2f} ГБ — это обрыв, "
                           "нужен повторный снимок с ящика"),
                "messages": scan["messages"], "size": scan["size"]}

    if dry_run:
        return {"ok": True, "dry_run": True, "messages": scan["messages"], "size": scan["size"]}

    index_path = snapshot_dir / INDEX_FILENAME
    with index_path.open("w", encoding="utf-8") as fh:
        for entry in scan["entries"]:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    archive, digest = storage.archive_snapshot(snapshot_dir)
    storage.write_manifest(
        snapshot_dir,
        org_id=org_id, login=login, reason="dismissal",
        folders_count=0, messages=scan["messages"], size_bytes=scan["size"],
        archive_name=archive.name, archive_sha256=digest,
        extra={
            "rebuilt_from_mbox": True,
            "rebuilt_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "rebuilt_note": ("снимок достроен из mail.mbox: архив и манифест отсутствовали, "
                             "повторное чтение ящика по IMAP не выполнялось"),
        })

    return {"ok": storage.verify_snapshot(snapshot_dir), "messages": scan["messages"],
            "size": scan["size"], "sha256": digest, "archive": archive.name}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", default="", help="ID организации (по умолчанию из .env)")
    ap.add_argument("--login", default="", help="достроить лучший каталог указанного логина")
    ap.add_argument("--all-missing", action="store_true",
                    help="достроить логины, у которых нет ни одного валидного снимка")
    ap.add_argument("--list", action="store_true", help="только показать незавершённые каталоги")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="достраивать даже при признаках обрыва")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from core.config import get_settings

    cfg = get_settings()
    org_id = args.org or cfg.yandex_org_id
    storage = get_storage()
    groups = incomplete_dirs(org_id)

    # Наличие архива и манифеста проверяем без чтения архивов: хеширование
    # сотен гигабайт по NFS заняло бы часы и здесь не нужно.
    def has_ready(login: str) -> bool:
        return any(
            s.with_suffix(".tar.gz").exists() and (s / "manifest.json").exists()
            for s in storage.list_backups(org_id, login)
        )

    valid_logins = {p.name for p in (storage.root / org_id).iterdir() if p.is_dir() and has_ready(p.name)}

    if args.list or not (args.login or args.all_missing):
        for login, dirs in sorted(groups.items()):
            best = max(dirs, key=lambda d: (d / "mail.mbox").stat().st_size if (d / "mail.mbox").exists() else 0)
            size = (best / "mail.mbox").stat().st_size if (best / "mail.mbox").exists() else 0
            mark = "есть валидный снимок" if login in valid_logins else "ВАЛИДНОГО СНИМКА НЕТ"
            print(f"   {login:<22} каталогов {len(dirs)}; лучший {best.name} "
                  f"({size / 2**20:.0f} МБ) — {mark}")
        return 0

    targets: list[tuple[str, Path]] = []
    if args.login:
        dirs = groups.get(args.login) or []
        if not dirs:
            print(f"незавершённых каталогов для {args.login} нет")
            return 1
        best = max(dirs, key=lambda d: (d / "mail.mbox").stat().st_size if (d / "mail.mbox").exists() else 0)
        targets.append((args.login, best))
    else:
        for login, dirs in sorted(groups.items()):
            if login in valid_logins:
                continue
            best = max(dirs, key=lambda d: (d / "mail.mbox").stat().st_size if (d / "mail.mbox").exists() else 0)
            targets.append((login, best))

    print(f"К достройке: {len(targets)} снимк(ов)" + (" (проверка, без записи)" if args.dry_run else ""))
    failed = 0
    for login, snapshot_dir in targets:
        started = time.time()
        result = finalize(snapshot_dir, args.dry_run, args.force)
        if result.get("ok"):
            print(f"[ OK ] {login:<22} {snapshot_dir.name}: писем {result['messages']}, "
                  f"{result['size'] / 2**20:.0f} МБ, sha256 {str(result.get('sha256'))[:16]}…, "
                  f"{time.time() - started:.0f} с")
        else:
            failed += 1
            print(f"[FAIL] {login:<22} {snapshot_dir.name}: {result.get('reason')}")
            if "dry-run" in str(result.get("reason")):
                print(f"        писем бы проиндексировано: {result.get('messages')}")
    print(f"Готово: успешно {len(targets) - failed}, с ошибкой {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
