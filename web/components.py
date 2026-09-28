"""Переиспользуемые компоненты: карточки, бейджи, таблицы, фоновые задачи.

Все элементы стилизованы через классы темы (y-*) и не зависят от страниц.
"""
from __future__ import annotations

import inspect
import queue
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from nicegui import run, ui

from web.theme import RUN_REASON_LABELS, STATUS_META

# Квазар-цвета для бейджей статусов (slot body-cell-*)
_QUASAR_COLORS = {
    "neutral": "grey", "info": "cyan", "success": "green",
    "warning": "amber", "danger": "red", "accent": "indigo", "violet": "purple",
}

# Слот таблицы для колонки статуса: ожидает значение из status_cell()
STATUS_SLOT = (
    '<q-td :props="props">'
    '  <q-badge outline :color="props.value.color" :label="props.value.label" />'
    '</q-td>'
)


# ---------- Форматирование ----------

def fmt_dt(value: Any, with_time: bool = True) -> str:
    """Дата/время из ISO-строки или datetime в «26.09.2026 14:05»."""
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")
    return str(value)


def fmt_size(num_bytes: Any) -> str:
    try:
        size = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.1f} {unit}" if unit != "Б" else f"{int(size)} Б"
        size /= 1024
    return f"{size:.1f} ГБ"


def run_reason_label(reason: str) -> str:
    return RUN_REASON_LABELS.get(reason, reason or "—")


# ---------- Мелкие элементы ----------

def badge(text: str, kind: str = "neutral", icon: Optional[str] = None) -> ui.row:
    with ui.row().classes(f"y-badge y-badge-{kind}") as el:
        if icon:
            ui.icon(icon).classes("text-[13px]")
        ui.label(text)
    return el


def status_badge(status: str) -> ui.row:
    """Бейдж статуса жизненного цикла (new/backed_up/deleted/...)."""
    label, kind, icon = STATUS_META.get(status, (status or "—", "neutral", "help"))
    return badge(label, kind, icon)


def status_cell(status: str) -> dict:
    """Значение для колонки таблицы со слотом STATUS_SLOT."""
    label, kind, _icon = STATUS_META.get(status, (status or "—", "neutral", "help"))
    color = _QUASAR_COLORS.get(kind, "grey")
    return {"label": label, "color": color}


def empty_state(text: str, icon: str = "inbox") -> None:
    with ui.column().classes("w-full items-center py-10 gap-2 text-slate-500"):
        ui.icon(icon).classes("text-4xl opacity-60")
        ui.label(text).classes("text-sm")


def stat_card(icon: str, label: str, value: Any, sub: str = "",
              tone: str = "indigo", href: Optional[str] = None) -> ui.card:
    """Карточка-метрика: иконка, подпись, крупное значение, пояснение."""
    card = ui.card().classes("y-card flex-1 min-w-[190px] p-4 gap-2 cursor-default")
    with card:
        with ui.row().classes("w-full items-center justify-between no-wrap gap-2"):
            with ui.column().classes("gap-0 min-w-0"):
                ui.label(label).classes("y-kv-label")
                ui.label(str(value)).classes("text-2xl font-bold leading-tight truncate")
            with ui.element("div").classes(f"y-stat-icon y-tone-{tone}"):
                ui.icon(icon).classes("text-[24px]")
        if sub:
            ui.label(sub).classes("text-xs text-slate-400")
    if href:
        card.classes("cursor-pointer")
        card.on("click", lambda: ui.navigate.to(href))
    return card


def section_title(title: str, subtitle: str = "", icon: Optional[str] = None) -> None:
    with ui.row().classes("items-center gap-2 no-wrap"):
        if icon:
            ui.icon(icon).classes("text-indigo-300 text-[22px]")
        with ui.column().classes("gap-0"):
            ui.label(title).classes("y-section-title")
            if subtitle:
                ui.label(subtitle).classes("text-xs text-slate-400")


def kv(label: str, value: Any, mono: bool = False) -> None:
    with ui.column().classes("gap-0 min-w-[120px]"):
        ui.label(label).classes("y-kv-label")
        ui.label(str(value)).classes("text-sm" + (" y-mono" if mono else ""))


# ---------- Таблицы ----------

def data_table(
    columns: list[dict],
    rows: Iterable[dict],
    *,
    title: str = "",
    subtitle: str = "",
    search: bool = True,
    rows_per_page: int = 15,
    actions: Optional[list[tuple[str, str, str, str]]] = None,
    on_action: Optional[Callable[[str, dict], None]] = None,
    selection: Optional[str] = None,
    row_key: str = "id",
) -> ui.table:
    """Стандартная таблица: шапка, поиск, счётчик и (опционально) кнопки-действия.

    actions — список (имя_события, иконка, цвет, подсказка).
    """
    rows = [dict(r) for r in rows]
    columns = [dict(c) for c in columns]
    if actions:
        columns = columns + [{"name": "actions", "label": "", "field": "actions", "align": "right"}]

    with ui.card().classes("y-card w-full p-0 overflow-hidden"):
        with ui.row().classes("w-full items-center justify-between px-4 py-3 gap-2 flex-wrap no-wrap-md"):
            with ui.column().classes("gap-0 min-w-0"):
                if title:
                    ui.label(title).classes("y-section-title")
                if subtitle:
                    ui.label(subtitle).classes("text-xs text-slate-400")
            count_label = ui.label(f"{len(rows)} записей").classes("text-xs text-slate-400 y-mono ml-auto")
            search_input = ui.input(placeholder="Поиск…").props("dense outlined clearable").classes("w-60") if search else None

        table = ui.table(
            columns=columns, rows=rows, row_key=row_key,
            pagination={"rowsPerPage": rows_per_page, "sortBy": columns[0]["name"] if columns else None},
            selection=selection,
        ).classes("y-table w-full").props("flat dense")
        if not rows:
            empty_state("Пока нет данных")

    if search_input is not None:
        def apply_filter() -> None:
            text = (search_input.value or "").strip().lower()
            table.rows = rows if not text else [
                r for r in rows if text in " ".join(str(v) for v in r.values()).lower()
            ]
            table.update()
            count_label.set_text(
                f"{len(table.rows)} из {len(rows)}" if text else f"{len(rows)} записей"
            )
        search_input.on_value_change(lambda _: apply_filter())

    if actions and on_action:
        btns = "".join(
            f'<q-btn dense flat round size="sm" icon="{icon}" color="{color}" '
            f"@click=\"() => $parent.$emit('{name}', props.row)\"><q-tooltip>{tip}</q-tooltip></q-btn>"
            for name, icon, color, tip in actions
        )
        table.add_slot(
            "body-cell-actions",
            f'<q-td :props="props" class="text-right whitespace-nowrap">{btns}</q-td>',
        )
        for name, *_ in actions:
            async def _dispatch(e, n=name) -> None:
                result = on_action(n, e.args)
                if inspect.isawaitable(result):
                    await result

            table.on(name, _dispatch)
    return table


# ---------- Диалоги ----------

def confirm(title: str, text: str, on_yes: Callable[[], Any], *, danger: bool = False) -> None:
    """Диалог подтверждения. on_yes может быть обычной или async-функцией."""
    dialog = ui.dialog()

    async def _accepted() -> None:
        dialog.close()
        result = on_yes()
        if inspect.isawaitable(result):
            await result

    with dialog, ui.card().classes("y-card p-5 gap-3 w-[440px] max-w-full"):
        ui.label(title).classes("text-lg font-semibold")
        ui.label(text).classes("text-sm text-slate-400")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")
            ui.button("Подтвердить", on_click=_accepted).props(
                "color=negative" if danger else "color=primary"
            )
    dialog.open()


# ---------- Фоновые задачи с журналом ----------

async def run_task(title: str, worker: Callable[[Callable], Any], *, description: str = "") -> Any:
    """Выполняет блокирующую работу в фоне, показывая диалог с журналом.

    worker(log_func) — функция, получающая log_func(msg, level='info') и
    возвращающая результат. UI-обновления безопасны: журнал наполняется через
    ui.timer, а не из рабочего потока.
    """
    log_queue: queue.Queue[tuple[str, str]] = queue.Queue()

    dialog = ui.dialog().props("persistent")
    with dialog, ui.card().classes("y-card w-[760px] max-w-full gap-3 p-5"):
        with ui.row().classes("items-center gap-3 no-wrap"):
            icon_box = ui.element("div").classes("y-stat-icon y-tone-indigo")
            with icon_box:
                ui.spinner(size="22px").classes("text-indigo-300")
            ui.label(title).classes("text-lg font-semibold")
        if description:
            ui.label(description).classes("text-sm text-slate-400")
        log_box = ui.log(max_lines=600).classes("y-log w-full h-72 rounded-xl bg-black/40 p-2")
    dialog.open()

    def log_func(msg: str, level: str = "info") -> None:
        log_queue.put((level, str(msg)))

    def drain() -> None:
        while not log_queue.empty():
            level, msg = log_queue.get()
            prefix = {"error": "✗ ", "warning": "⚠ "}.get(level, "• ")
            log_box.push(prefix + msg)

    timer = ui.timer(0.15, drain)
    ok = True
    try:
        result = await run.io_bound(worker, log_func)
    except Exception as exc:  # noqa: BLE001
        ok = False
        result = None
        log_func(f"ОШИБКА: {exc}", "error")
        ui.notify(f"Ошибка: {exc}", type="negative", multi_line=True)
    finally:
        timer.cancel()
        drain()

    icon_box.classes(remove="y-tone-indigo", add="y-tone-emerald" if ok else "y-tone-rose")
    with icon_box:
        ui.icon("check_circle" if ok else "error").classes("text-[22px]")
    with ui.row().classes("w-full justify-end"):
        ui.button("Закрыть", on_click=dialog.close).props("flat")
    if ok:
        ui.notify("Готово", type="positive")
    return result


def table_from_rows(rows: list[dict], columns: list[tuple[str, str]]) -> list[dict]:
    """Готовит строки для ui.table: [(ключ, подпись)] -> [{name,label,field}] + rows."""
    return [
        {"name": key, "label": label, "field": key, "align": "left", "sortable": True}
        for key, label in columns
    ]
