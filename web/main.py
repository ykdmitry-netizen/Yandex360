"""Точка входа консоли Y360 Admin: маршруты страниц и запуск NiceGUI.

Запуск: python web/main.py (из корня проекта) или scripts/start-stack.bat.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Позволяем запускать файл как скрипт: корень проекта должен быть в sys.path.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nicegui import app, ui  # noqa: E402

from core.config import get_settings  # noqa: E402
from core.logs import get_logger  # noqa: E402
from web import auth  # noqa: E402
from web.layout import page_shell  # noqa: E402
from web.pages import (  # noqa: E402
    backup as backup_page,
    dashboard,
    logs,
    mismatches,
    restore,
    retention,
    settings_page,
    sync,
    users,
)

logger = get_logger(__name__)

# Аутентификация консоли: при AUTH_MODE=none поведение не меняется.
auth.install(app)


@ui.page("/")
def page_dashboard() -> None:
    with page_shell("Дашборд", active="/", subtitle="состояние синхронизации и архивации"):
        dashboard.render()


@ui.page("/sync")
def page_sync() -> None:
    with page_shell("Мастер синхронизации", active="/sync", subtitle="данные → план → карточки → события"):
        sync.render()


@ui.page("/mismatches")
def page_mismatches() -> None:
    with page_shell("Расхождения", active="/mismatches", subtitle="AD ↔ Яндекс 360"):
        mismatches.render()


@ui.page("/users")
def page_users() -> None:
    with page_shell("Сотрудники", active="/users", subtitle="справочник и снимки ящиков"):
        users.render()


@ui.page("/backup")
def page_backup() -> None:
    with page_shell("Архивация", active="/backup", subtitle="уволенные, снимки и очистка"):
        backup_page.render()


@ui.page("/restore")
def page_restore() -> None:
    with page_shell("Восстановление", active="/restore", subtitle="просмотр снимков и возврат почты"):
        restore.render()


@ui.page("/retention")
def page_retention() -> None:
    with page_shell("Сроки хранения", active="/retention", subtitle="правила по должностям"):
        retention.render()


@ui.page("/logs")
def page_logs() -> None:
    with page_shell("Журнал", active="/logs", subtitle="аудит, снимки и сообщения задач"):
        logs.render()


@ui.page("/settings")
def page_settings() -> None:
    with page_shell("Настройки", active="/settings", subtitle="окружение и параметры"):
        settings_page.render()


def main() -> None:
    settings = get_settings()
    try:
        from core.db import init_schema

        init_schema()
        logger.info("Схема БД проверена/создана")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Схема БД не инициализирована при старте: %s", exc)
    logger.info("Консоль запускается: http://%s:%s", settings.ui_host, settings.ui_port)
    ui.run(
        host=settings.ui_host,
        port=settings.ui_port,
        title=settings.ui_title,
        dark=True,
        favicon="🛡️",
        reload=False,
        show=False,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()