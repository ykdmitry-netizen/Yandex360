"""Настройки: окружение, параметры поведения и сервисные проверки."""
from __future__ import annotations

from nicegui import ui

from core.config import get_settings
from core.db import db_available
from services import directory, state
from web.components import (
    badge,
    kv,
    run_task,
    section_title,
)


def _mode_badge(mock: bool) -> None:
    badge("мок" if mock else "боевое", "warning" if mock else "success", "science" if mock else "cloud_done")


def render() -> None:
    cfg = get_settings()

    def check_worker(log):
        from core.ad import ad_client_from_settings
        from integrations.yandex import yandex_client_from_settings

        log(f"PostgreSQL {cfg.pg_host}:{cfg.pg_port}/{cfg.pg_database}…")
        if db_available():
            log("OK: соединение установлено")
        else:
            log("ОШИБКА: база недоступна", "error")

        log(f"Active Directory: {cfg.ad_server} ({cfg.ad_base_dn})…")
        try:
            ad = ad_client_from_settings()
            ok = ad.test_connection()
            if ok:
                log("OK: LDAP отвечает")
            else:
                log(f"ОШИБКА: {getattr(ad, 'last_error', 'нет ответа')}", "error")
        except Exception as exc:  # noqa: BLE001
            log(f"ОШИБКА: {exc}", "error")

        log(f"Яндекс 360 API: {cfg.yandex_api_base or 'api360.yandex.net'}…")
        try:
            org = yandex_client_from_settings().get_org() or {}
            name = org.get("name") or org.get("displayName") or cfg.yandex_org_id
            log(f"OK: организация «{name}»")
        except Exception as exc:  # noqa: BLE001
            log(f"ОШИБКА: {exc}", "error")

        log("Проверка завершена")

    async def check_connections() -> None:
        await run_task("Проверка подключений", check_worker,
                       description="PostgreSQL, Active Directory (LDAP) и Directory API Яндекс 360.")

    async def sync_directory() -> None:
        def worker(log):
            log("Читаю пользователей из Directory API Яндекс 360…")
            count = directory.sync_users()
            log(f"Обновлено записей: {count}")
            return count

        await run_task("Синхронизация справочника", worker)

    @ui.refreshable
    def db_settings_section() -> None:
        try:
            retention_value = int(state.get_setting("retention_days", "") or cfg.retention_days)
        except Exception:  # noqa: BLE001
            retention_value = cfg.retention_days
        try:
            mailbox_value = state.get_setting("restore_storage_mailbox", "") or cfg.restore_storage_mailbox
        except Exception:  # noqa: BLE001
            mailbox_value = cfg.restore_storage_mailbox

        with ui.row().classes("gap-8 flex-wrap items-end"):
            retention_input = ui.number("Хранить снимки, дней", value=retention_value, min=1).classes("w-56")
            mailbox_input = ui.input("Ящик-хранилище (аудит)", value=mailbox_value).classes("w-96").props(
                "outlined dense"
            )

            def save() -> None:
                try:
                    state.set_setting("retention_days", str(int(retention_input.value or cfg.retention_days)))
                    state.set_setting("restore_storage_mailbox", (mailbox_input.value or "").strip())
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"Не удалось сохранить: {exc}", type="negative")
                    return
                ui.notify("Настройки сохранены в БД", type="positive")

            ui.button("Сохранить", icon="save", on_click=save).props("unelevated color=primary")
        ui.label("Срок хранения переопределяет .env для фоновой очистки снимков").classes(
            "text-xs text-slate-500"
        )

    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
        section_title("Настройки", "окружение и параметры консоли", "settings")
        with ui.row().classes("gap-2 flex-wrap"):
            ui.button("Проверить подключения", icon="cable", on_click=check_connections).props(
                "unelevated color=primary"
            )
            ui.button("Синхронизировать справочник", icon="cloud_sync", on_click=sync_directory).props("outline")

    with ui.row().classes("w-full gap-4 items-start flex-wrap"):
        with ui.card().classes("y-card p-4 gap-3 grow min-w-[360px]"):
            with ui.row().classes("w-full items-center justify-between"):
                section_title("Подключения", "источники данных и хранилище", "lan")
                _mode_badge(cfg.mock_ad and cfg.mock_imap and cfg.mock_yandex)
            with ui.row().classes("gap-8 flex-wrap"):
                kv("PostgreSQL", f"{cfg.pg_host}:{cfg.pg_port} / {cfg.pg_database}", mono=True)
                kv("Пользователь БД", cfg.pg_user, mono=True)
                kv("Хранилище снимков", str(cfg.backup_root), mono=True)
            ui.separator().classes("opacity-10")
            with ui.row().classes("gap-8 flex-wrap"):
                kv("Active Directory", cfg.ad_server, mono=True)
                kv("Base DN", cfg.ad_base_dn, mono=True)
                kv("СНИЛС-атрибут", cfg.ad_snils_attribute, mono=True)
            ui.separator().classes("opacity-10")
            with ui.row().classes("gap-8 flex-wrap"):
                kv("IMAP", cfg.imap_host, mono=True)
                kv("Порт IMAP", cfg.imap_port)
                kv("Яндекс 360 API", cfg.yandex_api_base or "api360.yandex.net", mono=True)
                kv("Организация", cfg.yandex_org_id, mono=True)
            ui.separator().classes("opacity-10")
            with ui.row().classes("gap-8 flex-wrap"):
                kv("Консоль", f"{cfg.ui_host}:{cfg.ui_port}", mono=True)
                kv("Заголовок", cfg.ui_title)

        with ui.card().classes("y-card p-4 gap-3 grow min-w-[360px]"):
            section_title("Поведение", "значения .env и переопределения в БД", "tune")
            with ui.row().classes("gap-8 flex-wrap"):
                kv("Срок хранения (env)", f"{cfg.retention_days} дн.")
                kv("Удаление ящика", f"{cfg.delete_mailbox_after_days} дн." if cfg.delete_mailbox_after_days else "—")
                kv("Параллельные запросы", cfg.parallel_requests)
                kv("Таймаут запроса", f"{cfg.request_timeout} с")
                kv("Лимит пользователей", cfg.max_users or "без ограничения")
                kv("Локальные email", "разрешены" if cfg.allow_local_emails else "запрещены")
            ui.separator().classes("opacity-10")
            ui.label("Переопределения в PostgreSQL").classes("y-kv-label")
            db_settings_section()

    with ui.card().classes("y-card p-4 gap-2 w-full"):
        section_title("О консоли", "Y360 Admin", "info")
        ui.label(
            "Единая консоль администрирования: синхронизация Active Directory → Яндекс 360 "
            "(отделы, карточки, контакты), архивация ящиков уволенных по IMAP с MBOX-снимками, "
            "сроки хранения по должностям, восстановление писем и сквозной аудит действий."
        ).classes("text-sm text-slate-400 max-w-4xl")
        with ui.row().classes("gap-2 flex-wrap"):
            badge("NiceGUI", "accent", "web")
            badge("PostgreSQL", "info", "storage")
            badge("LDAP / LDAPS", "violet", "dns")
            badge("IMAP XOAUTH2", "success", "mail")
