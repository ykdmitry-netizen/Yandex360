"""Дашборд: ключевые метрики, статусы ящиков и последние снимки."""
from __future__ import annotations

from collections import Counter

from nicegui import ui

from core.db import db_available, snapshot_age_minutes
from services import directory, dismissals, integration, pipeline, state, tasks
from web.components import (
    STATUS_SLOT,
    data_table,
    empty_state,
    fmt_dt,
    fmt_size,
    run_reason_label,
    run_task,
    section_title,
    stat_card,
    status_cell,
)
from web.layout import warn_banner
from web.theme import STATUS_META

_STATUS_COLORS = {
    "new": "#38bdf8",
    "detected": "#f59e0b",
    "accepted": "#6366f1",
    "backed_up": "#10b981",
    "delete_scheduled": "#8b5cf6",
    "deleted": "#64748b",
    "dismissed": "#34d399",
}

_RUN_COLUMNS = [
    {"name": "login", "label": "Логин", "field": "login", "align": "left", "sortable": True},
    {"name": "reason", "label": "Причина", "field": "reason", "align": "left"},
    {"name": "status", "label": "Статус", "field": "status", "align": "left"},
    {"name": "messages", "label": "Писем", "field": "messages", "align": "right"},
    {"name": "size", "label": "Размер", "field": "size", "align": "right"},
    {"name": "started_at", "label": "Запущен", "field": "started_at", "align": "left", "sortable": True},
]


def render() -> None:
    async def action_collect() -> None:
        await run_task(
            "Сбор данных: AD и Яндекс 360",
            lambda log: pipeline.collect_data(log_func=log),
            description="Читает пользователей из AD и Directory API и сохраняет снимок в PostgreSQL.",
        )
        overview.refresh()

    async def action_discover() -> None:
        def worker(log):
            log("Читаю пользователей из Directory API Яндекс 360…")
            result = tasks.discover_disabled()
            log(f"Пользователей в справочнике: {result['synced_users']}; новых уволенных: {result['new_dismissals']}")
            return result

        await run_task(
            "Поиск уволенных по статусу",
            worker,
            description="Пользователи со статусом disabled превращаются в карточки архивации.",
        )
        overview.refresh()

    async def action_process() -> None:
        def worker(log):
            log("Делаю снимки ящиков уволенных, у которых ещё нет архива…")
            result = tasks.process_pending()
            log(f"Снимков сделано: {result['dispatched']} из {result['total']}")
            return result

        await run_task("Архивация ожидающих", worker)
        overview.refresh()

    @ui.refreshable
    def overview() -> None:
        if not db_available():
            warn_banner("PostgreSQL недоступна — запустите стек: scripts/start-stack.bat.")
            return
        try:
            users = directory.list_users()
            statuses = integration.fetch_statuses()
            dms = dismissals.list_dismissals(limit=1000)
            runs = state.recent_runs(limit=8)
            age = snapshot_age_minutes()
        except Exception as exc:  # noqa: BLE001
            warn_banner(f"Не удалось прочитать данные из PostgreSQL: {exc}")
            return

        counts = Counter(statuses.values())
        archived = counts.get("backed_up", 0) + counts.get("delete_scheduled", 0)
        pending = sum(1 for d in dms if not d.get("backup_done_at"))

        if age is None:
            age_text, age_sub = "нет", "снимок ещё не делался"
        elif age < 90:
            age_text, age_sub = f"{int(age)} мин", "снимок свежий"
        else:
            age_text, age_sub = f"{age / 60:.1f} ч", "снимок стоит обновить"

        with ui.row().classes("w-full gap-4 flex-wrap"):
            stat_card("group", "Сотрудники", len(users), "учётные записи Яндекс 360", "indigo", "/users")
            stat_card("pending_actions", "Ожидают архивации", pending, "уволенные без снимка", "amber", "/backup")
            stat_card("archive", "В архиве", archived, "ящиков со снимками", "emerald", "/backup")
            stat_card("delete_forever", "Удалено", counts.get("deleted", 0), "ящиков после хранения", "rose", "/backup")
            stat_card("update", "Свежесть снимка", age_text, age_sub, "sky", "/sync")

        with ui.row().classes("w-full gap-4 items-start"):
            with ui.card().classes("y-card p-4 gap-2 w-full md:w-[380px] flex-none"):
                section_title("Статусы ящиков", "жизненный цикл архивации", "donut_small")
                chart_data = [
                    {
                        "name": STATUS_META.get(status, (status,))[0],
                        "value": count,
                        "itemStyle": {"color": _STATUS_COLORS.get(status, "#64748b")},
                    }
                    for status, count in counts.most_common()
                ]
                if not chart_data:
                    empty_state("Событий пока нет", "pie_chart")
                else:
                    options = {
                        "tooltip": {"trigger": "item"},
                        "legend": {
                            "bottom": 0,
                            "icon": "circle",
                            "textStyle": {"color": "#94a3b8", "fontSize": 11},
                        },
                        "series": [{
                            "type": "pie",
                            "radius": ["46%", "70%"],
                            "center": ["50%", "44%"],
                            "label": {"show": False},
                            "itemStyle": {"borderRadius": 6, "borderColor": "#0b1120", "borderWidth": 2},
                            "data": chart_data,
                        }],
                    }
                    ui.echart(options).classes("w-full h-64")

            with ui.element("div").classes("w-full grow min-w-0"):
                rows = [
                    {
                        "login": r.get("login") or "—",
                        "reason": run_reason_label(r.get("reason")),
                        "status": status_cell(r.get("status")),
                        "messages": r.get("messages") or 0,
                        "size": fmt_size(r.get("size_bytes")),
                        "started_at": fmt_dt(r.get("started_at")),
                    }
                    for r in runs
                ]
                table = data_table(
                    _RUN_COLUMNS, rows,
                    title="Последние снимки",
                    subtitle="журнал backup_runs",
                    search=False,
                    rows_per_page=8,
                )
                table.add_slot("body-cell-status", STATUS_SLOT)

    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
        section_title("Дашборд", "состояние синхронизации и архивации", "space_dashboard")
        with ui.row().classes("gap-2 flex-wrap"):
            ui.button("Обновить", icon="refresh", on_click=overview.refresh).props("flat")
            ui.button("Собрать данные", icon="cloud_download", on_click=action_collect).props("unelevated color=primary")
            ui.button("Найти уволенных", icon="person_search", on_click=action_discover).props("outline")
            ui.button("Архивировать ожидающих", icon="archive", on_click=action_process).props("outline")

    overview()
