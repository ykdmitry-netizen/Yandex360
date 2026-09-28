#!/usr/bin/env python3
"""Повторный снимок ящиков (архив был потерян или оборван).

Используется, когда снимок нельзя достроить из mbox — например, выгрузка
прервалась в начале и в каталоге лежит обрезанный mbox. Скрипт подключается
к ящику по IMAP (token-exchange) и делает полноценный снимок заново.

Запуск (лучше в фоне, ящики бывают по 10+ ГБ):
    nohup venv/bin/python scripts/resnapshot_users.py vanchikova.v gaulika.a \
        > /tmp/resnapshot.log 2>&1 &
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logins", nargs="+", help="логины сотрудников")
    ap.add_argument("--reason", default="dismissal", help="причина снимка (dismissal/manual/...)")
    ap.add_argument("--pause", type=int, default=15, help="пауза между ящиками, секунд")
    args = ap.parse_args()

    from core.config import get_settings
    from services import backup

    cfg = get_settings()
    print(f"Повторный снимок: {len(args.logins)} ящик(ов), организация {cfg.yandex_org_id}", flush=True)
    ok = failed = 0
    for login in args.logins:
        started = time.time()
        try:
            result = backup.perform_user_backup(cfg.yandex_org_id, login, args.reason)
            size = (result.get("size_bytes") or 0) / 2**30
            print(f"[OK] {login}: писем {result.get('messages')}, {size:.2f} ГБ, "
                  f"sha256 {str(result.get('sha256'))[:16]}… ({time.time() - started:.0f} с)", flush=True)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {login}: {type(exc).__name__}: {exc} ({time.time() - started:.0f} с)", flush=True)
            failed += 1
        time.sleep(args.pause)
    print(f"Итого: успешно {ok}, ошибок {failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
