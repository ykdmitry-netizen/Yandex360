"""Журнал: аудит действий, снимки ящиков и журнал бэкапов."""
from __future__ import annotations

import json

from nicegui import ui

from core.db import get_audit_log, query_all
from services import state
from web.components import (
    STATUS_SLOT,
    data_table,
    empty_state,
    fmt_dt,
    fmt_size,
    run_reason_label,
    section_title,
    status_cell,
)
from web.layout import warn_banner

_AUDIT_COLUMNS = [
    {"name": "created_at", "label": "Время", "field": "created_at", "align": "left", "sortable": True},
    {"name": "operation", "label": "Операция", "field": "operation", "align": "left", "sortable": True},
    {"name": "target_login", "label": "Объект", "field": "target_login", "align": "left"},
    {"name": "result", "label": "Результат", "field": "result", "align": "left"},
    {"name": "error", "label": "Ошибка", "field": "error", "align": "left"},
]

_RUN_COLUMNS = [
    {"name": "id", "label": "#", "field": "id", "align": "left"},
    {"name": "login", "label": "Логин", "field": "login", "align": "left", "sortable": True},
    {"name": "reason", "label": "Причина", "field": "reason", "align": "left"},
    {"name": "status", "label": "Статус", "field": "status", "align": "left"},
    {"name": "messages", "label": "Писем", "field": "messages", "align": "right"},
    {"name": "size", "label": "Размер", "field": "size", "align": "right"},
    {"name": "started_at", "label": "Запущен", "field": "started_at", "align": "left", "sortable": True},
    {"name": "finished_at", "label": "Завершён", "field": "finished_at", "align": "left"},
]


def render() -> None:
    def audit_result_cell(row: dict) -> dict:
        if row.get("success"):
            label, color = ("Предпросмотр", "amber") if row.get("dry_run") else ("Успех", "green")
        else:
            label, color = "Ошибка", "red"
        return {"label": label, "color": color}

    def show_details(row: dict) -> None:
        dialog = ui.dialog()
        with dialog, ui.card().classes("y-card p-5 gap-3 w-[860px] max-w-full"):
            ui.label(f"{row.get('operation') or '—'} · {row.get('target_login') or '—'}").classes(
                "text-lg font-semibold"
            )
            ui.label(fmt_dt(row.get("created_at"))).classes("text-xs text-slate-400")
            with ui.row().classes("w-full gap-4 items-start flex-wrap"):
                with ui.column().classes("gap-1 grow min-w-[320px]"):
                    ui.label("До").classes("y-kv-label")
                    ui.code(json.dumps(row.get("before_data") or {}, ensure_ascii=False, indent=2)).classes(
                        "w-full y-mono text-xs"
                    )
                with ui.column().classes("gap-1 grow min-w-[320px]"):
                    ui.label("После").classes("y-kv-label")
                    ui.code(json.dumps(row.get("after_data") or {}, ensure_ascii=False, indent=2)).classes(
                        "w-full y-mono text-xs"
                    )
            with ui.row().classes("w-full justify-end"):
                ui.button("Закрыть", on_click=dialog.close).props("flat")
        dialog.open()

    async def on_audit_action(name: str, row: dict) -> None:
        if name == "details":
            show_details(row)

    try:
        operations = [r["operation"] for r in query_all("SELECT DISTINCT operation FROM audit_log ORDER BY 1")]
    except Exception:  # noqa: BLE001
        operations = []

    @ui.refreshable
    def audit_body() -> None:
        try:
            rows = get_audit_log(
                limit=int(limit_select.value or 200),
                operation=op_select.value if op_select.value != "(все)" else None,
                only_failed=bool(failed_switch.value),
                search=(search_input.value or "").strip() or None,
            )
        except Exception as exc:  # noqa: BLE001
            warn_banner(f"Не удалось прочитать аудит: {exc}")
            return
        if not rows:
            empty_state("Записей нет — измените фильтры", "search_off")
            return
        data_table(
            _AUDIT_COLUMNS,
            [
                {
                    "id": r.get("id"),
                    "created_at": fmt_dt(r.get("created_at")),
                    "operation": r.get("operation") or "—",
                    "target_login": r.get("target_login") or "—",
                    "result": audit_result_cell(r),
                    "error": r.get("error_message") or "",
                    "before_data": r.get("before_data"),
                    "after_data": r.get("after_data"),
                }
                for r in rows
            ],
            title="Аудит действий",
            subtitle="все изменения пользователей, отделов, снимков и восстановлений",
            rows_per_page=20,
            actions=[("details", "visibility", "grey", "Детали (до / после)")],
            on_action=on_audit_action,
        ).add_slot("body-cell-result", STATUS_SLOT)

    @ui.refreshable
    def runs_body() -> None:
        try:
            rows = state.recent_runs(limit=200)
        except Exception as exc:  # noqa: BLE001
            warn_banner(f"Не удалось прочитать журнал снимков: {exc}")
            return
        table = data_table(
            _RUN_COLUMNS,
            [
                {
                    "id": r.get("id"),
                    "login": r.get("login") or "—",
                    "reason": run_reason_label(r.get("reason")),
                    "status": status_cell(r.get("status")),
                    "messages": r.get("messages") or 0,
                    "size": fmt_size(r.get("size_bytes")),
                    "started_at": fmt_dt(r.get("started_at")),
                    "finished_at": fmt_dt(r.get("finished_at")),
                }
                for r in rows
            ],
            title="Снимки ящиков",
            subtitle="таблица backup_runs",
            rows_per_page=20,
        )
        table.add_slot("body-cell-status", STATUS_SLOT)

    @ui.refreshable
    def logs_body() -> None:
        try:
            rows = state.recent_logs(limit=500)
        except Exception as exc:  # noqa: BLE001
            warn_banner(f"Не удалось прочитать журнал: {exc}")
            return
        data_table(
            [
                {"name": "ts", "label": "Время", "field": "ts", "align": "left", "sortable": True},
                {"name": "run_id", "label": "Снимок #", "field": "run_id", "align": "left"},
                {"name": "level", "label": "Уровень", "field": "level", "align": "left"},
                {"name": "message", "label": "Сообщение", "field": "message", "align": "left"},
            ],
            [
                {
                    "ts": fmt_dt(r.get("ts")),
                    "run_id": r.get("run_id") or "—",
                    "level": r.get("level") or "info",
                    "message": r.get("message") or "",
                }
                for r in rows
            ],
            title="Журнал снимков",
            subtitle="подробные сообщения фоновых задач (backup_logs)",
            rows_per_page=25,
        )

    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
        section_title("Журнал", "аудит, снимки и сообщения задач", "receipt_long")
        with ui.row().classes("gap-2"):
            ui.button("Обновить", icon="refresh", on_click=lambda: (audit_body.refresh(), runs_body.refresh(),
                                                                   logs_body.refresh())).props("flat")

    with ui.tabs().classes("w-full") as tabs:
        t_audit = ui.tab("Аудит", icon="verified_user")
        t_runs = ui.tab("Снимки", icon="photo_camera")
        t_logs = ui.tab("Сообщения", icon="list_alt")

    with ui.tab_panels(tabs, value=t_audit).classes("w-full"):
        with ui.tab_panel(t_audit):
            with ui.row().classes("items-center gap-3 flex-wrap py-1"):
                search_input = ui.input("Поиск по объекту/ошибке").classes("w-72").props("outlined dense clearable")
                op_select = ui.select(["(все)"] + operations, value="(все)", label="Операция").classes(
                    "w-56"
                ).props("outlined dense")
                limit_select = ui.select(["100", "200", "500", "1000"], value="200", label="Записей").classes(
                    "w-28"
                ).props("outlined dense")
                failed_switch = ui.switch("Только ошибки")
            for widget in (search_input, op_select, limit_select):
                widget.on_value_change(lambda: audit_body.refresh())
            failed_switch.on_value_change(lambda: audit_body.refresh())
            audit_body()
        with ui.tab_panel(t_runs):
            runs_body()
        with ui.tab_panel(t_logs):
            logs_body()
