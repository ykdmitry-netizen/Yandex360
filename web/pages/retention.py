"""Сроки хранения: реестр правил по должностям и предпросмотр."""
from __future__ import annotations

from nicegui import ui

from services import retention
from web.components import (
    STATUS_SLOT,
    badge,
    confirm,
    data_table,
    fmt_dt,
    kv,
    section_title,
)
from web.layout import warn_banner

_COLUMNS = [
    {"name": "name", "label": "Правило", "field": "name", "align": "left", "sortable": True},
    {"name": "keywords", "label": "Должности (ключевые слова)", "field": "keywords", "align": "left"},
    {"name": "term", "label": "Срок хранения", "field": "term", "align": "left"},
    {"name": "priority", "label": "Приоритет", "field": "priority", "align": "right"},
    {"name": "active", "label": "Активно", "field": "active", "align": "left"},
    {"name": "notes", "label": "Заметка", "field": "notes", "align": "left"},
]


def _active_cell(active: bool) -> dict:
    return {"label": "Включено" if active else "Выключено", "color": "green" if active else "grey"}


def render() -> None:
    def rule_dialog(rule: retention.RetentionRule | None = None) -> None:
        dialog = ui.dialog()
        with dialog, ui.card().classes("y-card p-5 gap-3 w-[580px] max-w-full"):
            ui.label("Новое правило хранения" if rule is None else f"Правило: {rule.name}").classes(
                "text-lg font-semibold"
            )
            name = ui.input("Название", value=rule.name if rule else "").classes("w-full").props("outlined dense")
            keywords = ui.input("Ключевые слова (через запятую)", value=rule.keywords if rule else "").classes(
                "w-full"
            ).props("outlined dense")
            ui.label("Пустые ключевые слова делают правило запасным (для всех остальных должностей)").classes(
                "text-xs text-slate-500"
            )
            with ui.row().classes("gap-3 flex-wrap"):
                amount = ui.number("Срок", value=rule.delete_after_amount if rule else 14, min=1).classes("w-28")
                unit = ui.select({"days": "дней", "months": "месяцев"},
                                 value=rule.delete_after_unit if rule else "days",
                                 label="Единица").classes("w-40")
                priority = ui.number("Приоритет", value=rule.priority if rule else 100, min=0,
                                     hint="меньше = применяется раньше").classes("w-40")
            active = ui.switch("Включено", value=rule.active if rule else True)
            notes = ui.input("Заметка", value=(rule.notes or "") if rule else "").classes("w-full").props(
                "outlined dense"
            )

            def save() -> None:
                try:
                    retention.save_rule(
                        name=(name.value or "").strip(),
                        keywords=keywords.value or "",
                        delete_after_amount=int(amount.value or 0),
                        delete_after_unit=str(unit.value or "days"),
                        priority=int(priority.value or 100),
                        active=bool(active.value),
                        notes=(notes.value or "").strip() or None,
                        rule_id=rule.id if rule else None,
                    )
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"Не удалось сохранить: {exc}", type="negative")
                    return
                ui.notify("Правило сохранено", type="positive")
                dialog.close()
                body.refresh()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Отмена", on_click=dialog.close).props("flat")
                ui.button("Сохранить", icon="save", on_click=save).props("unelevated color=primary")
        dialog.open()

    def toggle_rule(rule_id: int, active: bool) -> None:
        try:
            retention.set_active(rule_id, not active)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Ошибка: {exc}", type="negative")
            return
        body.refresh()

    def delete_rule(rule_id: int, name: str) -> None:
        def do_delete() -> None:
            try:
                retention.delete_rule(rule_id)
                ui.notify("Правило удалено", type="positive")
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"Ошибка: {exc}", type="negative")
            body.refresh()

        confirm(
            "Удалить правило?",
            f"Правило «{name}» будет удалено. Уже назначенные сроки у уволенных не изменятся.",
            do_delete,
            danger=True,
        )

    def on_action(name: str, row: dict) -> None:
        rule_id = row.get("id")
        if rule_id is None:
            return
        if name == "edit":
            try:
                rule = retention.get_rule(int(rule_id))
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"Ошибка: {exc}", type="negative")
                return
            if rule:
                rule_dialog(rule)
        elif name == "toggle":
            toggle_rule(int(rule_id), bool(row.get("_active")))
        elif name == "delete":
            delete_rule(int(rule_id), row.get("name") or "")

    @ui.refreshable
    def body() -> None:
        try:
            rules = retention.load_rules(active_only=False)
        except Exception as exc:  # noqa: BLE001
            warn_banner(f"Не удалось прочитать правила: {exc}")
            return
        active_count = sum(1 for r in rules if r.active)
        fallback = next((r for r in rules if r.active and r.is_fallback), None)
        with ui.row().classes("gap-2 flex-wrap"):
            badge(f"Правил: {len(rules)}", "accent", "policy")
            badge(f"Включено: {active_count}", "success", "check_circle")
            if fallback:
                badge(f"Запасное правило: {fallback.name} · {fallback.label}", "info", "shield")
            else:
                badge("Запасное правило не задано", "warning", "warning")
        rows = [
            {
                "id": r.id,
                "_active": r.active,
                "name": r.name + (" (запасное)" if r.is_fallback else ""),
                "keywords": r.keywords if not r.is_fallback else "— все остальные должности —",
                "term": r.label,
                "priority": r.priority,
                "active": _active_cell(r.active),
                "notes": r.notes or "",
            }
            for r in rules
        ]
        table = data_table(
            _COLUMNS, rows,
            title="Реестр правил",
            subtitle="срок считается от даты снимка; выигрывает правило с меньшим приоритетом",
            rows_per_page=15,
            actions=[
                ("edit", "edit", "primary", "Редактировать"),
                ("toggle", "power_settings_new", "grey", "Включить / выключить"),
                ("delete", "delete", "negative", "Удалить"),
            ],
            on_action=on_action,
        )
        table.add_slot("body-cell-active", STATUS_SLOT)

    @ui.refreshable
    def preview_section() -> None:
        with ui.row().classes("items-end gap-3 flex-wrap"):
            pos_input = ui.input("Должность сотрудника", placeholder="Например: Руководитель отдела продаж").classes(
                "w-96"
            ).props("outlined dense")
            ui.button("Подобрать правило", icon="travel_explore", on_click=lambda: do_preview()).props(
                "unelevated color=primary"
            )
        preview_box = ui.column().classes("w-full gap-2")

        def do_preview() -> None:
            preview_box.clear()
            try:
                result = retention.preview(pos_input.value)
            except Exception as exc:  # noqa: BLE001
                with preview_box:
                    warn_banner(f"Не удалось подобрать правило: {exc}")
                return
            with preview_box:
                with ui.row().classes("items-center gap-3 flex-wrap"):
                    badge(result["rule_name"], "accent", "policy")
                    badge(f"Срок: {result['label']}", "info", "schedule")
                with ui.row().classes("gap-8 flex-wrap"):
                    kv("Удаление ящика", fmt_dt(result["deletion_at"], with_time=False))
                    kv("Совпадение", result["matched_by"])

    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
        section_title("Сроки хранения", "сколько хранить ящик после снимка", "schedule")
        with ui.row().classes("gap-2 flex-wrap"):
            ui.button("Обновить", icon="refresh", on_click=body.refresh).props("flat")
            ui.button("Добавить правило", icon="add", on_click=lambda: rule_dialog()).props("unelevated color=primary")

    body()

    with ui.card().classes("y-card p-4 gap-3 w-full"):
        section_title("Предпросмотр", "что получит сотрудник с такой должностью", "science")
        preview_section()
