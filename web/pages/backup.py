"""Архивация: жизненный цикл уволенных, снимки ящиков, события, очистка."""
from __future__ import annotations

from nicegui import ui

from core.config import get_settings
from services import backup, dismissals, integration, tasks
from web.components import (
    STATUS_SLOT,
    confirm,
    data_table,
    empty_state,
    fmt_dt,
    run_task,
    section_title,
    stat_card,
    status_cell,
)
from web.layout import warn_banner

_DISMISSAL_COLUMNS = [
    {"name": "user_name", "label": "ФИО", "field": "user_name", "align": "left", "sortable": True},
    {"name": "login", "label": "Логин", "field": "login", "align": "left", "sortable": True},
    {"name": "position", "label": "Должность", "field": "position", "align": "left"},
    {"name": "state", "label": "Статус", "field": "state", "align": "left"},
    {"name": "fired_at", "label": "Уволен", "field": "fired_at", "align": "left"},
    {"name": "backup_done_at", "label": "Снимок", "field": "backup_done_at", "align": "left"},
    {"name": "retention", "label": "Хранение", "field": "retention", "align": "left"},
    {"name": "delete_after", "label": "Удаление", "field": "delete_after", "align": "left"},
]

_EVENT_COLUMNS = [
    {"name": "login", "label": "Логин", "field": "login", "align": "left", "sortable": True},
    {"name": "display_name", "label": "Имя", "field": "display_name", "align": "left"},
    {"name": "status", "label": "Статус", "field": "status", "align": "left"},
    {"name": "created_at", "label": "Создано", "field": "created_at", "align": "left"},
    {"name": "notes", "label": "Примечание", "field": "notes", "align": "left"},
]


def render() -> None:
    cfg = get_settings()

    async def discover() -> None:
        def worker(log):
            log("Читаю пользователей из Directory API…")
            result = tasks.discover_disabled()
            log(f"Пользователей: {result['synced_users']}; новых уволенных: {result['new_dismissals']}")
            return result

        await run_task("Поиск уволенных", worker, description="Статус disabled в Яндекс 360 → карточка архивации.")
        body.refresh()

    async def import_events() -> None:
        def worker(log):
            log("Принимаю события «только в Яндексе» в жизненный цикл…")
            result = tasks.import_events()
            log(f"Импортировано: {result.get('imported', 0)}, пропущено: {result.get('skipped', 0)}")
            return result

        await run_task("Импорт событий", worker, description="sync_events → карточки архивации (идемпотентно).")
        body.refresh()

    async def process_pending() -> None:
        def worker(log):
            log("Делаю снимки ящиков уволенных, у которых нет архива…")
            result = tasks.process_pending()
            log(f"Снимков сделано: {result['dispatched']} из {result['total']}")
            return result

        await run_task("Снимки для ожидающих", worker, description="Последовательная архивация всех ожидающих.")
        body.refresh()

    async def backup_all() -> None:
        def worker(log):
            log("Снимаю ящики всех сотрудников (последовательно)…")
            result = backup.backup_all(cfg.yandex_org_id)
            log(f"Готово: {result.get('dispatched', 0)} из {result.get('total', 0)}")
            return result

        await run_task("Снимок всех ящиков", worker, description="reason=scheduled, каждый ящик в свой каталог.")
        body.refresh()

    async def cleanup() -> None:
        def worker(log):
            result = tasks.cleanup_dismissed()
            log(f"Удалено устаревших снимков: {result['removed_snapshots']} (хранение {result['retention_days']} дн.)")
            return result

        await run_task("Очистка старых снимков", worker)
        body.refresh()

    async def make_snapshot(login: str) -> None:
        def worker(log):
            log(f"Снимок ящика {login}…")
            result = backup.perform_user_backup(cfg.yandex_org_id, login, "dismissal")
            log(f"Писем: {result.get('messages', 0)}")
            return result

        await run_task(f"Снимок: {login}", worker)
        body.refresh()

    def note_dialog(user_id: str, login: str, note: str) -> None:
        dialog = ui.dialog()
        with dialog, ui.card().classes("y-card p-5 gap-3 w-[520px] max-w-full"):
            ui.label(f"Заметка: {login}").classes("text-lg font-semibold")
            textarea = ui.textarea(value=note or "").props("outlined autogrow").classes("w-full")

            def save() -> None:
                try:
                    dismissals.update_notes(user_id, textarea.value or "")
                    ui.notify("Заметка сохранена", type="positive")
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"Не удалось сохранить: {exc}", type="negative")
                dialog.close()
                body.refresh()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Отмена", on_click=dialog.close).props("flat")
                ui.button("Сохранить", icon="save", on_click=save).props("unelevated color=primary")
        dialog.open()

    def mark_deleted(user_id: str, login: str) -> None:
        def do_mark() -> None:
            try:
                dismissals.mark_deleted(user_id)
                ui.notify(f"{login}: ящик отмечен удалённым", type="positive")
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"Ошибка: {exc}", type="negative")
            body.refresh()

        confirm(
            "Отметить ящик удалённым?",
            f"Ящик {login} будет помечен как удалённый в Яндекс 360. Запись попадёт в аудит.",
            do_mark,
            danger=True,
        )

    async def on_action(name: str, row: dict) -> None:
        login = row.get("login")
        user_id = row.get("user_id")
        if name == "backup" and login:
            await make_snapshot(login)
        elif name == "deleted" and user_id:
            mark_deleted(user_id, login or user_id)
        elif name == "note" and user_id:
            note_dialog(user_id, login or user_id, row.get("notes") or "")

    @ui.refreshable
    def body() -> None:
        try:
            dms = dismissals.list_dismissals(limit=500)
            events = integration.list_events(limit=500)
        except Exception as exc:  # noqa: BLE001
            warn_banner(f"Не удалось прочитать данные архивации: {exc}")
            return

        states = {d.get("user_id"): dismissals.dismissal_state(d) for d in dms}
        pending = sum(1 for s in states.values() if s in ("detected", "accepted"))
        archived = sum(1 for s in states.values() if s == "backed_up")
        scheduled = sum(1 for s in states.values() if s == "delete_scheduled")
        deleted = sum(1 for s in states.values() if s == "deleted")

        with ui.row().classes("w-full gap-4 flex-wrap"):
            stat_card("pending_actions", "Ожидают снимка", pending, "уволенные без архива", "amber")
            stat_card("photo_camera", "Снимок сделан", archived, "ждут срока удаления", "emerald")
            stat_card("event", "Удаление запланировано", scheduled, "срок хранения идёт", "violet")
            stat_card("delete_forever", "Удалено", deleted, "ящиков очищено", "slate")
            stat_card("campaign", "События", len(events), "sync_events всего", "sky")

        with ui.tabs().classes("w-full") as tabs:
            t_dms = ui.tab(f"Уволенные ({len(dms)})")
            t_ev = ui.tab(f"События ({len(events)})")

        with ui.tab_panels(tabs, value=t_dms).classes("w-full"):
            with ui.tab_panel(t_dms):
                if not dms:
                    empty_state("Карточек архивации нет — нажмите «Найти уволенных»", "person_search")
                else:
                    rows = [
                        {
                            "user_id": d.get("user_id"),
                            "login": d.get("login") or "—",
                            "user_name": d.get("user_name") or "—",
                            "position": d.get("position") or "—",
                            "state": status_cell(states.get(d.get("user_id")) or "detected"),
                            "fired_at": fmt_dt(d.get("fired_at"), with_time=False),
                            "backup_done_at": fmt_dt(d.get("backup_done_at"), with_time=False),
                            "retention": d.get("retention_rule") or "—",
                            "delete_after": fmt_dt(d.get("retention_until"), with_time=False),
                            "notes": d.get("notes") or "",
                        }
                        for d in dms
                    ]
                    table = data_table(
                        _DISMISSAL_COLUMNS, rows,
                        title="Карточки архивации",
                        subtitle="жизненный цикл: обнаружен → снимок → удаление",
                        rows_per_page=15,
                        actions=[
                            ("backup", "photo_camera", "primary", "Сделать снимок ящика"),
                            ("deleted", "delete_forever", "negative", "Отметить ящик удалённым"),
                            ("note", "edit_note", "grey", "Заметка"),
                        ],
                        on_action=on_action,
                    )
                    table.add_slot("body-cell-state", STATUS_SLOT)
            with ui.tab_panel(t_ev):
                if not events:
                    empty_state("Событий нет — события создаёт публикация «только в Яндексе»", "campaign")
                else:
                    table = data_table(
                        _EVENT_COLUMNS,
                        [
                            {
                                "login": e.get("login") or "—",
                                "display_name": e.get("display_name") or "—",
                                "status": status_cell(e.get("status")),
                                "created_at": fmt_dt(e.get("created_at")),
                                "notes": e.get("notes") or "",
                            }
                            for e in events
                        ],
                        title="События архивации",
                        subtitle="кандидаты и их текущее состояние",
                        rows_per_page=15,
                    )
                    table.add_slot("body-cell-status", STATUS_SLOT)

    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
        section_title("Архивация", "уволенные, снимки ящиков и очистка", "archive")
        with ui.row().classes("gap-2 flex-wrap"):
            ui.button("Обновить", icon="refresh", on_click=body.refresh).props("flat")
            ui.button("Найти уволенных", icon="person_search", on_click=discover).props("unelevated color=primary")
            ui.button("Импорт событий", icon="campaign", on_click=import_events).props("outline")
            ui.button("Снимки для ожидающих", icon="photo_camera", on_click=process_pending).props("outline")
            ui.button("Снимок всех активных", icon="cloud_upload", on_click=lambda: confirm(
                "Сделать снимки всех ящиков?",
                "Будут последовательно заархивированы ящики всех сотрудников организации. "
                "Это может занять время; прогресс виден в журнале.",
                backup_all,
            )).props("outline")
            ui.button("Очистить старые", icon="cleaning_services", on_click=cleanup).props("outline")

    body()
