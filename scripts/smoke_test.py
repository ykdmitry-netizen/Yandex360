"""Сквозная проверка стенда: моки + PostgreSQL + сервисы (без UI).

Запуск из корня проекта:
    venv\\Scripts\\python.exe scripts\\smoke_test.py

Скрипт безопасен для локального стенда: работает с мок-AD и мок-IMAP,
данные пишутся только в локальную БД y360_admin и data/backups.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAILED: list[str] = []


def check(name: str, func) -> object:
    try:
        result = func()
    except Exception as exc:  # noqa: BLE001
        FAILED.append(name)
        print(f"[FAIL] {name}: {exc}")
        return None
    print(f"[ OK ] {name}: {result}")
    return result


def main() -> int:
    from core.config import get_settings
    from core.db import db_available, get_audit_log, init_schema, snapshot_age_minutes
    from services import backup, directory, dismissals, pipeline, retention, state, tasks

    cfg = get_settings()
    silent = lambda *_a, **_k: None  # noqa: E731

    print("== Y360 Admin: сквозная проверка ==")
    print(f"Стенд: AD={cfg.ad_server}, IMAP={cfg.imap_host}, API={cfg.yandex_api_base}")

    if not check("PostgreSQL доступна", db_available):
        print("База недоступна — запустите scripts/start-stack.bat")
        return 1
    check("Схема БД", lambda: init_schema() or "применена")

    check("Directory API: синхронизация справочника", directory.sync_users)
    check("Сбор данных (AD + Яндекс)", lambda: _short(pipeline.collect_data(log_func=silent)))
    check("Отчёт о расхождениях", lambda: pipeline.diff_report()["counts"])
    check("План отделов", lambda: _plan_counts(pipeline.build_department_plan(log_func=silent)))
    check("Синхронизация карточек (DRY-RUN)",
          lambda: _sync_counts(pipeline.sync_users(apply=False, log_func=silent)))
    check("Автообнаружение уволенных", tasks.discover_disabled)
    check("Импорт событий в архив", tasks.import_events)

    users = check("Справочник сотрудников", lambda: [u.login for u in directory.list_users()])
    if users:
        login = users[0]
        check(f"Снимок ящика {login} (вручную)",
              lambda: _backup_result(backup.perform_user_backup(cfg.yandex_org_id, login, "manual")))

    pending = dismissals.find_not_backed_up()
    print(f"      уволенных без снимка: {len(pending)}")
    if pending:
        login = pending[0]["login"]
        check(f"Снимок уволенного {login}",
              lambda: _backup_result(backup.perform_user_backup(cfg.yandex_org_id, login, "dismissal")))

    check("Правило хранения для «Генеральный директор»", lambda: retention.preview("Генеральный директор"))
    check("Запуски бэкапов", lambda: len(state.recent_runs(20)))
    check("Записи аудита", lambda: len(get_audit_log(limit=20)))
    check("Свежесть снимка, мин", lambda: round(snapshot_age_minutes() or -1, 1))
    check("Проверка целостности снимков", lambda: _verify_all(cfg.yandex_org_id))

    print("=" * 46)
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)} -> {FAILED}")
        return 1
    print("Все проверки пройдены")
    return 0


def _short(result: dict) -> dict:
    return {k: result[k] for k in ("yandex_users", "ad_users", "departments", "snapshot_saved")}


def _plan_counts(plan: dict) -> dict:
    return {key: len(items) for key, items in plan.items()}


def _sync_counts(result: dict) -> dict:
    return {k: result[k] for k in ("updated", "skipped", "errors")}


def _backup_result(result: dict) -> dict:
    return {k: result.get(k) for k in ("messages", "size_bytes", "sha256")}


def _verify_all(org_id: str) -> str:
    from services.storage import get_storage

    storage = get_storage()
    org_dir = storage.root / org_id
    total = verified = 0
    for login_dir in sorted(p for p in org_dir.iterdir() if p.is_dir()):
        for snapshot in storage.list_backups(org_id, login_dir.name):
            total += 1
            if storage.verify_snapshot(snapshot):
                verified += 1
    return f"{verified}/{total} снимков подтверждены sha256"


if __name__ == "__main__":
    raise SystemExit(main())
