#!/usr/bin/env python3
"""Проверка нового контура на боевых кредах (только чтение, ничего не меняет
в Яндекс 360 и AD: все шаги синхронизации выполняются в режиме DRY-RUN).

Запуск на сервере:
    cd /opt/y360-admin && venv/bin/python scripts/check_production.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def brief(value, limit: int = 200) -> str:
    """Короткое представление результата: большие словари не заливают лог."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"… (обрезано, всего {len(text)} симв.)"


def step(name: str, func):
    started = time.time()
    try:
        result = func()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        return None
    print(f"[ OK ] {name} ({time.time() - started:.1f} с): {brief(result)}")
    return result


def main() -> int:
    from services import directory, dismissals, pipeline, state, tasks

    print("=== Y360 Admin: проверка на боевых данных (DRY-RUN) ===")

    step("Справочник сотрудников из Directory API", directory.sync_users)
    step("Организация", lambda: directory.get_org().get("name"))
    step("Отделы из БД", lambda: len(directory.list_departments()))

    collected = step("Сбор данных (Яндекс + AD → снимок БД)",
                     lambda: pipeline.collect_data(log_func=lambda *_: None))
    if collected:
        print(f"        Яндекс: {collected.get('yandex_users')}, AD: {collected.get('ad_users')}, "
              f"отделов: {collected.get('departments')}, снимок сохранён: {collected.get('snapshot_saved')}")

    report = step("Отчёт о расхождениях", pipeline.diff_report)
    if report:
        print(f"        {report.get('counts')}")

    plan = step("План отделов", lambda: pipeline.build_department_plan(log_func=lambda *_: None))
    if plan:
        print(f"        { {k: len(v) for k, v in plan.items()} }")

    sync = step("Синхронизация карточек (DRY-RUN, без изменений)",
                lambda: pipeline.sync_users(apply=False, log_func=lambda *_: None))
    if sync:
        print(f"        обновить: {sync.get('updated')}, без изменений: {sync.get('skipped')}, "
              f"ошибок: {sync.get('errors')}, без отдела: {len(sync.get('blocked_no_department') or [])}")

    step("Автообнаружение уволенных", tasks.discover_disabled)
    step("Импорт событий в архив", tasks.import_events)

    pending = step("Уволенные без снимка", lambda: len(dismissals.find_not_backed_up()))
    step("Запусков бэкапа в БД", lambda: len(state.recent_runs(500)))
    step("Записей аудита", lambda: len(state.recent_logs(500)))
    status = step("Снимок БД", pipeline.snapshot_status)
    if status:
        print(f"        источники: { {k: v.get('count') for k, v in status['snapshot'].items()} }, "
              f"записей аудита: {len(status.get('audit') or [])}")

    users = directory.list_users()
    print(f"        всего сотрудников в БД: {len(users)}; уволенных без снимка: {pending}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
