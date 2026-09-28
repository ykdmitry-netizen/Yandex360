"""Расхождения: сравнение AD и Яндекс 360 по снимку, экспорт в Excel."""
from __future__ import annotations

from nicegui import ui

from services import export, pipeline
from web.components import (
    data_table,
    empty_state,
    run_task,
    section_title,
    stat_card,
)
from web.layout import warn_banner


def _export_worker(log):
    from core.compare import compare_users

    log("Читаю снимок из PostgreSQL…")
    yandex_users, ad_users, _depts = pipeline.load_snapshot_or_raise()
    log(f"Пользователи: Яндекс 360 — {len(yandex_users)}, AD — {len(ad_users)}")
    if not yandex_users and not ad_users:
        raise pipeline.PipelineError("Снимок пуст — сначала выполните «Собрать данные»")
    log("Сравниваю данные…")
    comparison = compare_users(yandex_users, ad_users)
    log("Формирую .xlsx…")
    data = export.build_report(yandex_users, ad_users, comparison)
    log(f"Готово: {len(data) // 1024} КБ")
    return {"data": data, "name": export.report_filename()}


def render() -> None:
    state: dict = {"report": None, "error": None}

    def build_report() -> None:
        try:
            state["report"] = pipeline.diff_report()
            state["error"] = None
        except Exception as exc:  # noqa: BLE001
            state["error"] = str(exc)
        report_section.refresh()

    async def download_report() -> None:
        result = await run_task(
            "Экспорт отчёта в Excel",
            _export_worker,
            description="Соберёт пользователей и расхождения в .xlsx и скачает файл.",
        )
        if result:
            ui.download(result["data"], result["name"])

    @ui.refreshable
    def report_section() -> None:
        if state["error"]:
            warn_banner(f"Не удалось построить отчёт: {state['error']}")
        report = state["report"]
        if not report:
            empty_state("Постройте отчёт — сравнение по последнему снимку", "difference")
            return
        counts = report.get("counts") or {}
        with ui.row().classes("w-full gap-4 flex-wrap"):
            stat_card("cloud", "Яндекс 360", counts.get("yandex", 0), "пользователей", "indigo")
            stat_card("dns", "Active Directory", counts.get("ad", 0), "пользователей", "sky")
            stat_card("task_alt", "Совпало", counts.get("matched", 0), "карточек без расхождений", "emerald")
            stat_card("difference", "Расхождения", counts.get("mismatched", 0), "карточек требуют правки", "amber")
            stat_card("person_off", "Только в AD", counts.get("only_in_ad", 0), "нет в Яндексе", "rose")
            stat_card("person_search", "Только в Яндексе", counts.get("only_in_yandex", 0), "нет в AD", "violet")

        mismatched = report.get("mismatched") or []
        diff_rows = [
            {
                "login": item.get("login") or "—",
                "name": item.get("name") or "—",
                "field": d.get("label") or d.get("field") or "—",
                "ad": str(d.get("ad") or "—"),
                "yandex": str(d.get("yandex") or "—"),
                "match_by": item.get("match_by") or "—",
            }
            for item in mismatched
            for d in (item.get("differences") or [])
        ]
        only_ad = report.get("only_in_ad") or []
        only_yandex = report.get("only_in_yandex") or []

        with ui.tabs().classes("w-full") as tabs:
            t_diff = ui.tab(f"Расхождения ({len(diff_rows)})")
            t_ad = ui.tab(f"Только в AD ({len(only_ad)})")
            t_yx = ui.tab(f"Только в Яндексе ({len(only_yandex)})")

        with ui.tab_panels(tabs, value=t_diff).classes("w-full"):
            with ui.tab_panel(t_diff):
                if not diff_rows:
                    empty_state("Расхождений нет — данные согласованы", "check_circle")
                else:
                    data_table(
                        [
                            {"name": "login", "label": "Логин", "field": "login", "align": "left", "sortable": True},
                            {"name": "name", "label": "ФИО", "field": "name", "align": "left"},
                            {"name": "field", "label": "Поле", "field": "field", "align": "left"},
                            {"name": "ad", "label": "AD", "field": "ad", "align": "left"},
                            {"name": "yandex", "label": "Яндекс 360", "field": "yandex", "align": "left"},
                            {"name": "match_by", "label": "Сопоставлен по", "field": "match_by", "align": "left"},
                        ],
                        diff_rows,
                        title="Расхождения по полям",
                        subtitle="что хранится в AD и что — в Яндекс 360",
                        rows_per_page=25,
                    )
            with ui.tab_panel(t_ad):
                if not only_ad:
                    empty_state("Все пользователи AD есть в Яндексе", "check_circle")
                else:
                    data_table(
                        [
                            {"name": "login", "label": "Логин", "field": "login", "align": "left", "sortable": True},
                            {"name": "name", "label": "ФИО", "field": "name", "align": "left"},
                            {"name": "department", "label": "Отдел", "field": "department", "align": "left"},
                            {"name": "title", "label": "Должность", "field": "title", "align": "left"},
                        ],
                        [{"login": i.get("login") or "—", "name": i.get("name") or "—",
                          "department": i.get("department") or "—", "title": i.get("title") or "—"} for i in only_ad],
                        title="Есть в AD, нет в Яндекс 360",
                        subtitle="нужно создать ящик или проверить сопоставление",
                        rows_per_page=25,
                    )
            with ui.tab_panel(t_yx):
                if not only_yandex:
                    empty_state("Лишних ящиков нет", "check_circle")
                else:
                    data_table(
                        [
                            {"name": "login", "label": "Логин", "field": "login", "align": "left", "sortable": True},
                            {"name": "name", "label": "ФИО", "field": "name", "align": "left"},
                            {"name": "email", "label": "Email", "field": "email", "align": "left"},
                            {"name": "department_name", "label": "Отдел", "field": "department_name", "align": "left"},
                            {"name": "position", "label": "Должность", "field": "position", "align": "left"},
                        ],
                        [{"login": i.get("login") or "—", "name": i.get("name") or "—", "email": i.get("email") or "—",
                          "department_name": i.get("department_name") or "—", "position": i.get("position") or "—"}
                         for i in only_yandex],
                        title="Есть в Яндекс 360, нет в AD",
                        subtitle="кандидаты на архивацию (кнопка «Опубликовать» в мастере)",
                        rows_per_page=25,
                    )

    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
        section_title("Расхождения", "AD ↔ Яндекс 360 по последнему снимку", "difference")
        with ui.row().classes("gap-2 flex-wrap"):
            ui.button("Построить отчёт", icon="play_arrow", on_click=build_report).props("unelevated color=primary")
            ui.button("Обновить", icon="refresh", on_click=build_report).props("flat")
            ui.button("Экспорт в Excel", icon="download", on_click=download_report).props("outline")

    report_section()
