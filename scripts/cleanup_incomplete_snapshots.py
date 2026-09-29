#!/usr/bin/env python3
"""Чистка незавершённых каталогов снимков (мусор от прерванных попыток).

Правило безопасности: каталог удаляется только если
  1) в нём нет ни архива .tar.gz, ни manifest.json (то есть снимок не готов);
  2) у ЭТОГО ЖЕ логина уже есть готовый снимок (архив + манифест) —
     значит каталог является дубликатом неудачной попытки;
  3) каталог не изменялся последние --min-age-hours часов (чтобы не удалить
     тот, который пишется прямо сейчас).

Если у логина готового снимка нет — каталог сохраняется: это единственные
данные по сотруднику.

Запуск:
    venv/bin/python scripts/cleanup_incomplete_snapshots.py            # только показать
    venv/bin/python scripts/cleanup_incomplete_snapshots.py --yes      # удалить
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def has_ready_snapshot(login_dir: Path) -> bool:
    for snap in login_dir.iterdir():
        if not snap.is_dir():
            continue
        if snap.with_suffix(".tar.gz").exists() and (snap / "manifest.json").exists():
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", default="", help="ID организации (по умолчанию из .env)")
    ap.add_argument("--min-age-hours", type=float, default=6.0,
                    help="не трогать каталоги, изменённые позже этого срока (по умолчанию 6 ч)")
    ap.add_argument("--yes", action="store_true", help="действительно удалить (без флага — только показать)")
    args = ap.parse_args()

    from core.config import get_settings
    from services.storage import get_storage

    cfg = get_settings()
    org_id = args.org or cfg.yandex_org_id
    storage = get_storage()
    root = storage.root / org_id
    if not root.exists():
        print(f"нет каталога {root}")
        return 1

    now = time.time()
    to_delete: list[tuple[Path, int, str]] = []
    keep: list[tuple[Path, str]] = []

    for login_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        ready = has_ready_snapshot(login_dir)
        for snap in sorted(p for p in login_dir.iterdir() if p.is_dir()):
            if snap.with_suffix(".tar.gz").exists() and (snap / "manifest.json").exists():
                continue  # готовый снимок
            if not ready:
                keep.append((snap, "единственные данные по логину — готового снимка нет"))
                continue
            age_hours = (now - snap.stat().st_mtime) / 3600
            if age_hours < args.min_age_hours:
                keep.append((snap, f"изменён {age_hours:.1f} ч назад — возможно, снимок идёт"))
                continue
            to_delete.append((snap, dir_size(snap), f"{age_hours:.0f} ч, дубликат готового снимка"))

    freed = sum(size for _, size, _ in to_delete)
    print(f"К удалению: {len(to_delete)} каталог(ов), {freed / 2**30:.2f} ГБ")
    for snap, size, reason in to_delete:
        print(f"   - {snap.parent.name}/{snap.name}  {size / 2**20:8.0f} МБ  ({reason})")
    print(f"Сохранено: {len(keep)} каталог(ов)")
    for snap, reason in keep[:15]:
        print(f"   = {snap.parent.name}/{snap.name}  — {reason}")

    if not to_delete:
        return 0
    if not args.yes:
        print("\nРежим проверки: ничего не удалено. Для удаления добавьте --yes")
        return 0

    removed = failed = 0
    for snap, _, _ in to_delete:
        try:
            shutil.rmtree(snap)
            snap.with_suffix(".tar.gz").unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            failed += 1
            print(f"   не удалось удалить {snap}: {exc}")
    print(f"\nУдалено: {removed}; ошибок: {failed}; освобождено {freed / 2**30:.2f} ГБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
