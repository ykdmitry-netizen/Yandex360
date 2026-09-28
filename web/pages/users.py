"""Сотрудники: справочник Яндекс 360, снимки ящиков по требованию, история."""
from __future__ import annotations

from nicegui import ui

from core.config import get_settings
from services import backup, directory, state
from web.components import (
    STATUS_SLOT,
    badge,
    data_table,
    empty_state,
    fmt_dt,
    fmt_size,
    run_reason_label,
    run_task,
    section_title,
    status_cell,
)
from web.layout import warn_banner

_COLUMNS = [
    {"name": "login", "label": "Логин", "field": "login", "align": "left", "sortable": True},
    {"name": "name", "label": "ФИО", "field": "name", "align": "left", "sortable": True},
    {"name": "position", "label": "Должность", "field": "position", "align": "left"},
    {"name": "email", "label": "Email", "field": "email", "align": "left"},
    {"name": "snils", "label": "СНИЛС", "field": "snils", "align": "left"},
    {"name": "status", "label": "Статус", "field": "status", "align": "left"},
]


def _user_status(status: str) -> dict:
    """Значение для колонки статуса учётной записи (слот STATUS_SLOT)."""
    value = (status or "").lower()
    if value == "active":
        return {"label": "Активен", "color": "green"}
    if value in ("disabled", "inactive", "suspended"):
        return {"label": "Отключён", "color": "amber"}
    return {"label": status or "—", "color": "grey"}


def render() -> None:
    cfg = get_settings()

    async def sync_directory() -> None:
        def worker(log):
            log("Читаю пользователей из Directory API Яндекс 360…")
            count = directory.sync_users()
            log(f"Обновлено записей: {count}")
            return count

        await run_task(
            "Синхронизация справочника",
            worker,
            description="Таблица users в PostgreSQL приводится в соответствие с Directory API.",
        )
        table_section.refresh()

    async def make_backup(login: str) -> None:
        def worker(log):
            log(f"Снимок ящика {login}…")
            result = backup.perform_user_backup(cfg.yandex_org_id, login, "manual")
            log(f"Писем: {result.get('messages', 0)}, размер: {fmt_size(result.get('size_bytes'))}")
            return result

        await run_task(
            f"Снимок ящика: {login}",
            worker,
            description="IMAP → MBOX → manifest + sha256 → запись в журнал backup_runs.",
        )
        table_section.refresh()

    def show_history(login: str) -> None:
        try:
            runs = state.runs_for_user(login, limit=50)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"История недоступна: {exc}", type="negative")
            return
        dialog = ui.dialog()
        with dialog, ui.card().classes("y-card p-5 gap-3 w-[820px] max-w-full"):
            ui.label(f"История снимков: {login}").classes("text-lg font-semibold")
            if not runs:
                empty_state("Снимков ещё не было", "history")
            else:
                rows = [
                    {
                        "id": r.get("id"),
                        "reason": run_reason_label(r.get("reason")),
                        "status": status_cell(r.get("status")),
                        "messages": r.get("messages") or 0,
                        "size": fmt_size(r.get("size_bytes")),
                        "started_at": fmt_dt(r.get("started_at")),
                        "finished_at": fmt_dt(r.get("finished_at")),
                        "error": r.get("error") or "",
                    }
                    for r in runs
                ]
                table = ui.table(
                    columns=[
                        {"name": "id", "label": "#", "field": "id", "align": "left"},
                        {"name": "reason", "label": "Причина", "field": "reason", "align": "left"},
                        {"name": "status", "label": "Статус", "field": "status", "align": "left"},
                        {"name": "messages", "label": "Писем", "field": "messages", "align": "right"},
                        {"name": "size", "label": "Размер", "field": "size", "align": "right"},
                        {"name": "started_at", "label": "Запущен", "field": "started_at", "align": "left"},
                        {"name": "finished_at", "label": "Завершён", "field": "finished_at", "align": "left"},
                        {"name": "error", "label": "Ошибка", "field": "error", "align": "left"},
                    ],
                    rows=rows,
                    row_key="id",
                    pagination={"rowsPerPage": 8},
                ).classes("y-table w-full").props("flat dense")
                table.add_slot("body-cell-status", STATUS_SLOT)
            with ui.row().classes("w-full justify-end"):
                ui.button("Закрыть", on_click=dialog.close).props("flat")
        dialog.open()

    async def on_action(name: str, row: dict) -> None:
        login = row.get("login")
        if not login:
            return
        if name == "backup":
            await make_backup(login)
        elif name == "history":
            show_history(login)

    @ui.refreshable
    def table_section() -> None:
        try:
            users = directory.list_users()
        except Exception as exc:  # noqa: BLE001
            warn_banner(f"Не удалось прочитать справочник: {exc}")
            return
        active = sum(1 for u in users if (u.status or "") == "active")
        with ui.row().classes("gap-2 flex-wrap"):
            badge(f"Всего: {len(users)}", "accent", "group")
            badge(f"Активные: {active}", "success", "check_circle")
            badge(f"Отключённые: {len(users) - active}", "warning", "block")
        rows = [
            {
                "login": u.login,
                "name": u.name or "—",
                "position": u.position or "—",
                "email": u.email or "—",
                "snils": u.snils or "—",
                "status": _user_status(u.status),
            }
            for u in users
        ]
        table = data_table(
            _COLUMNS, rows,
            title="Справочник сотрудников",
            subtitle="данные Directory API Яндекс 360 (таблица users)",
            rows_per_page=20,
            actions=[
                ("backup", "archive", "primary", "Сделать снимок ящика"),
                ("history", "history", "grey", "История снимков"),
            ],
            on_action=on_action,
        )
        table.add_slot("body-cell-status", STATUS_SLOT)

    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
        section_title("Сотрудники", "справочник, снимки ящиков и история", "group")
        with ui.row().classes("gap-2 flex-wrap"):
            ui.button("Обновить", icon="refresh", on_click=table_section.refresh).props("flat")
            ui.button("Синхронизировать справочник", icon="cloud_sync", on_click=sync_directory).props(
                "unelevated color=primary"
            )

    table_section()
