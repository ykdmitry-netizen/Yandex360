"""Мастер синхронизации: сбор данных, план отделов, синхронизация карточек."""
from __future__ import annotations

from nicegui import ui

from services import pipeline
from web.components import (
    badge,
    confirm,
    data_table,
    empty_state,
    fmt_dt,
    run_task,
    section_title,
)
from web.layout import warn_banner

_PLAN_TABS = [
    ("to_create", "Создать", [("name", "Отдел"), ("parent_id", "Организация (id)"), ("path", "Путь в AD")]),
    ("to_delete_duplicates", "Пустые дубли", [("name", "Отдел"), ("parent_name", "Организация"), ("id", "id отдела")]),
    ("orphan_users", "Без отдела в Яндексе", [("login", "Логин"), ("department", "Отдел в AD")]),
    ("to_reparent", "Не в той организации", [("name", "Отдел"), ("current_parent_id", "Текущий родитель"), ("wanted_parent_id", "Нужный родитель")]),
]


def _rows(items: list[dict], columns: list[tuple[str, str]]) -> list[dict]:
    return [{key: str(item.get(key) or "—") for key, _ in columns} for item in items]


def render() -> None:
    state: dict = {"plan": None, "apply": None, "sync": None, "publish": None}

    async def collect() -> None:
        await run_task(
            "Сбор данных: AD и Яндекс 360",
            lambda log: pipeline.collect_data(log_func=log),
            description="Читает пользователей из AD и Directory API, сохраняет снимок в PostgreSQL.",
        )
        snapshot_section.refresh()

    async def build_plan() -> None:
        result = await run_task(
            "План отделов",
            lambda log: pipeline.build_department_plan(log_func=log),
            description="Сравнивает OU из AD со структурой подразделений Яндекс 360.",
        )
        if result is not None:
            state["plan"] = result
            state["apply"] = None
        plan_section.refresh()

    async def apply_plan() -> None:
        plan = state["plan"] or {}
        result = await run_task(
            "Применение плана отделов",
            lambda log: pipeline.apply_department_plan(plan, apply=True, log_func=log),
            description="Создаёт отсутствующие отделы и удаляет пустые дубли.",
        )
        if result is not None:
            state["apply"] = result
        plan_section.refresh()

    async def sync_dry_run() -> None:
        result = await run_task(
            "Синхронизация: предпросмотр",
            lambda log: pipeline.sync_users(apply=False, log_func=log),
            description="DRY-RUN: показывает, какие карточки будут обновлены. Ничего не меняет.",
        )
        if result is not None:
            state["sync"] = result
        sync_section.refresh()

    async def sync_apply() -> None:
        result = await run_task(
            "Синхронизация: применение",
            lambda log: pipeline.sync_users(apply=True, log_func=log),
            description="Обновляет отдел, email, должность, СНИЛС и контакты в Яндекс 360.",
        )
        if result is not None:
            state["sync"] = result
        sync_section.refresh()

    async def publish() -> None:
        result = await run_task(
            "Публикация «только в Яндексе»",
            lambda log: pipeline.publish_only_yandex(log_func=log),
            description="Создаёт события архивации для сотрудников, которых нет в AD.",
        )
        if result is not None:
            state["publish"] = result
        publish_section.refresh()

    def confirm_apply() -> None:
        confirm(
            "Применить план отделов?",
            "Отделы будут созданы в Яндекс 360, пустые дубли — удалены. Действие попадает в аудит.",
            apply_plan,
        )

    def confirm_sync() -> None:
        confirm(
            "Применить синхронизацию?",
            "Карточки сотрудников в Яндекс 360 будут обновлены по данным AD. Все изменения попадут в аудит.",
            sync_apply,
        )

    @ui.refreshable
    def snapshot_section() -> None:
        try:
            st = pipeline.snapshot_status()
        except Exception as exc:  # noqa: BLE001
            warn_banner(f"Снимок недоступен: {exc}")
            return
        snap = st.get("snapshot") or {}
        titles = {"ad": "Active Directory", "yandex": "Яндекс 360", "dept": "Отделы"}
        with ui.row().classes("w-full gap-4 items-start"):
            with ui.card().classes("y-card p-4 gap-3 grow"):
                section_title("Снимок данных", "последние записи по источникам", "storage")
                if not snap:
                    empty_state("Снимка ещё нет — нажмите «Собрать данные»", "cloud_download")
                for source, info in snap.items():
                    with ui.row().classes("w-full items-center gap-3 no-wrap"):
                        ui.icon("dns").classes("text-indigo-300 text-[18px]")
                        ui.label(titles.get(source, source)).classes("text-sm w-44")
                        ui.label(f"{info.get('count', 0)} записей").classes("text-sm y-mono")
                        ui.label(fmt_dt(info.get("at"))).classes("text-xs text-slate-500 ml-auto")
            with ui.card().classes("y-card p-4 gap-2 w-full md:w-[400px] flex-none"):
                section_title("Журнал аудита", "последние операции", "history")
                audit = st.get("audit") or []
                if not audit:
                    empty_state("Операций пока нет", "history")
                for item in audit[:7]:
                    ok = item.get("success")
                    with ui.row().classes("items-center gap-2 no-wrap w-full"):
                        ui.icon("check_circle" if ok else "error").classes(
                            ("text-emerald-300" if ok else "text-rose-300") + " text-[16px]"
                        )
                        text = " ".join(str(x) for x in (item.get("operation"), item.get("target_login")) if x)
                        ui.label(text).classes("text-xs truncate")
                        ui.label(fmt_dt(item.get("created_at"), with_time=True)).classes(
                            "text-[11px] text-slate-500 ml-auto"
                        )

    @ui.refreshable
    def plan_section() -> None:
        plan = state["plan"]
        with ui.card().classes("y-card p-4 gap-3 w-full"):
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                section_title("План отделов", "OU из AD → подразделения Яндекс 360", "account_tree")
                with ui.row().classes("gap-2"):
                    apply_btn = ui.button("Применить план", icon="done_all", on_click=confirm_apply)
                    apply_btn.props("unelevated color=primary" if plan else "disable")
            if not plan:
                empty_state("Постройте план: «План отделов» в панели сверху", "account_tree")
                return
            counts = {key: len(plan.get(key) or []) for key, _, _ in _PLAN_TABS}
            with ui.row().classes("gap-2 flex-wrap"):
                badge(f"Создать: {counts['to_create']}", "accent", "add")
                badge(f"Пустые дубли: {counts['to_delete_duplicates']}", "warning", "content_copy")
                badge(f"Без отдела: {counts['orphan_users']}", "danger", "person_off")
                badge(f"Не в той организации: {counts['to_reparent']}", "violet", "swap_horiz")
            apply_result = state["apply"]
            if apply_result:
                ui.separator().classes("opacity-10")
                with ui.row().classes("gap-2 flex-wrap"):
                    badge(f"Создано: {apply_result.get('created', 0)}", "success", "check")
                    badge(f"Удалено дублей: {apply_result.get('deleted', 0)}", "success", "delete")
                    badge(f"Ошибок: {apply_result.get('errors', 0)}", "danger", "error")
            with ui.tabs().classes("w-full") as tabs:
                tab_els = []
                for key, label, _cols in _PLAN_TABS:
                    tab_els.append((key, ui.tab(f"{label} ({counts[key]})")))
            with ui.tab_panels(tabs, value=tab_els[0][1]).classes("w-full"):
                for (key, _label, columns), (_key, tab) in zip(_PLAN_TABS, tab_els):
                    with ui.tab_panel(tab):
                        items = plan.get(key) or []
                        if not items:
                            empty_state("Пусто", "check_circle")
                        else:
                            data_table(
                                [{"name": k, "label": lbl, "field": k, "align": "left"} for k, lbl in columns],
                                _rows(items, columns),
                                search=False,
                                rows_per_page=10,
                            )

    def confirm_apply() -> None:
        from web.components import confirm

        confirm(
            "Применить план отделов?",
            "Отделы будут созданы в Яндекс 360, пустые дубли — удалены. Действие попадает в аудит.",
            apply_plan,
        )

    @ui.refreshable
    def sync_section() -> None:
        result = state["sync"]
        with ui.card().classes("y-card p-4 gap-3 w-full"):
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                section_title("Синхронизация карточек", "AD → Яндекс 360: отдел, email, должность, СНИЛС, контакты", "sync_alt")
                with ui.row().classes("gap-2"):
                    ui.button("Предпросмотр (DRY-RUN)", icon="visibility", on_click=sync_dry_run).props("outline")
                    ui.button("Применить", icon="bolt", on_click=confirm_sync).props("unelevated color=primary")
            if not result:
                empty_state("Сначала сделайте предпросмотр: он покажет, что изменится", "visibility")
                return
            mode = "Предпросмотр" if not result.get("applied") else "Применено"
            with ui.row().classes("gap-2 flex-wrap"):
                badge(mode, "info" if not result.get("applied") else "success", "flag")
                badge(f"Обновлено: {result.get('updated', 0)}", "accent", "edit")
                badge(f"Без изменений: {result.get('skipped', 0)}", "neutral", "check")
                badge(f"Ошибок: {result.get('errors', 0)}", "danger", "error")
                badge(f"Без отдела: {len(result.get('blocked_no_department') or [])}", "warning", "rule")
            details = result.get("details") or []
            if details:
                data_table(
                    [
                        {"name": "login", "label": "Логин", "field": "login", "align": "left"},
                        {"name": "fields", "label": "Поля", "field": "fields", "align": "left"},
                        {"name": "mode", "label": "Режим", "field": "mode", "align": "left"},
                    ],
                    [
                        {
                            "login": d.get("login") or "—",
                            "fields": ", ".join(d.get("fields") or []),
                            "mode": "Предпросмотр" if d.get("dry_run") else "Применено",
                        }
                        for d in details
                    ],
                    title="Изменения",
                    rows_per_page=10,
                )
            blocked = result.get("blocked_no_department") or []
            if blocked:
                data_table(
                    [
                        {"name": "login", "label": "Логин", "field": "login", "align": "left"},
                        {"name": "department", "label": "Отдел в AD, которого нет в Яндексе", "field": "department", "align": "left"},
                    ],
                    [{"login": b.get("login") or "—", "department": b.get("department") or "—"} for b in blocked],
                    title="Требуют отдела в Яндексе",
                    subtitle="сначала создайте отдел (план выше), потом повторите синхронизацию",
                    rows_per_page=10,
                )

    @ui.refreshable
    def publish_section() -> None:
        result = state["publish"]
        with ui.card().classes("y-card p-4 gap-3 w-full"):
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                section_title("Только в Яндексе", "кандидаты на архивацию: есть в 360, нет в AD", "person_search")
                ui.button("Опубликовать", icon="campaign", on_click=publish).props("outline")
            if not result:
                ui.label(
                    "Сотрудники, которых нет в AD, будут созданы как события архивации. "
                    "Вернувшиеся в AD — автоматически закрываются."
                ).classes("text-xs text-slate-400")
                return
            with ui.row().classes("gap-2 flex-wrap"):
                badge(f"Кандидатов: {result.get('candidates', 0)}", "info", "group")
                badge(f"Опубликовано: {result.get('published', 0)}", "accent", "campaign")
                badge(f"Пропущено: {result.get('skipped', 0)}", "neutral", "redo")
                badge(f"Вернулись в AD: {result.get('closed', 0)}", "success", "undo")

    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
        section_title("Мастер синхронизации", "полный цикл: данные → план → карточки → события", "sync_alt")
        with ui.row().classes("gap-2 flex-wrap"):
            ui.button("Обновить", icon="refresh", on_click=snapshot_section.refresh).props("flat")
            ui.button("1. Собрать данные", icon="cloud_download", on_click=collect).props("unelevated color=primary")
            ui.button("2. План отделов", icon="account_tree", on_click=build_plan).props("outline")
            ui.button("3. Синхронизация", icon="sync_alt", on_click=sync_dry_run).props("outline")

    publish_section()
    snapshot_section()
    plan_section()
    sync_section()
