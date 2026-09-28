"""Проверка импорта всех модулей проекта Y360 Admin (без запуска UI).

Запуск:
    <repo>\\venv\\Scripts\\python.exe tests\\test_imports.py
"""
from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

# Корень репозитория: тесты лежат в <repo>/tests
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

MODULES = [
    "core.config", "core.logs", "core.models", "core.db", "core.compare",
    "core.ad", "core.ad_mock",
    "integrations.yandex", "integrations.mail_tokens",
    "services.state", "services.storage", "services.orgs", "services.directory",
    "services.export", "services.pipeline", "services.tasks", "services.backup",
    "services.dismissals", "services.retention", "services.restore",
    "services.integration", "services.imap_mock", "services.imap_backup",
    "services.imap_restore",
    "web.theme", "web.components", "web.layout",
    "web.pages.dashboard", "web.pages.sync", "web.pages.mismatches",
    "web.pages.users", "web.pages.backup", "web.pages.restore",
    "web.pages.retention", "web.pages.logs", "web.pages.settings_page",
    "mocks.mock_yandex360",
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ok, failed = 0, []
    for name in MODULES:
        try:
            importlib.import_module(name)
            print(f"[ OK ] import {name}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            print(f"[FAIL] import {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)
    print("=" * 50)
    print(f"Импортировано: {ok}/{len(MODULES)}; провалено: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
