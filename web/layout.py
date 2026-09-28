"""Каркас страницы: шапка, боковое меню, контейнер контента.

Каждая страница строится внутри page_shell(...): общий визуальный каркас
описан здесь один раз, а страницы отвечают только за своё содержимое.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from nicegui import ui

from core.config import get_settings
from web.theme import apply_theme

# Меню: группа -> [(подпись, адрес, иконка)]
NAV_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("Обзор", [
        ("Дашборд", "/", "space_dashboard"),
    ]),
    ("Синхронизация", [
        ("Мастер синхронизации", "/sync", "sync_alt"),
        ("Расхождения", "/mismatches", "difference"),
        ("Сотрудники", "/users", "group"),
    ]),
    ("Почта", [
        ("Архивация", "/backup", "archive"),
        ("Восстановление", "/restore", "unarchive"),
        ("Сроки хранения", "/retention", "schedule"),
    ]),
    ("Система", [
        ("Журнал", "/logs", "receipt_long"),
        ("Настройки", "/settings", "settings"),
    ]),
]


def _env_chip(label: str, value: str, mock: bool) -> None:
    """Индикатор окружения: мок — янтарный, боевое — зелёное."""
    kind = "warning" if mock else "success"
    with ui.row().classes(f"y-badge y-badge-{kind}").style("gap:6px"):
        ui.icon("science" if mock else "cloud_done").classes("text-[13px]")
        ui.label(f"{label}: {value}" if value else label).classes("text-[11px]")


def _nav_item(label: str, target: str, icon: str, active: str) -> None:
    classes = "y-nav"
    if target == active:
        classes += " y-nav-active"
    with ui.item(on_click=lambda t=target: ui.navigate.to(t)).classes(classes):
        with ui.item_section().props("avatar"):
            ui.icon(icon).classes("text-[20px]")
        with ui.item_section():
            ui.label(label).classes("text-sm")


def warn_banner(text: str, icon: str = "warning_amber") -> None:
    """Плашка-предупреждение (БД/API недоступны и т.п.)."""
    with ui.row().classes("y-card w-full items-center gap-3 p-4 no-wrap"):
        ui.icon(icon).classes("text-amber-300 text-[22px]")
        ui.label(text).classes("text-sm text-slate-300")


@contextmanager
def page_shell(title: str, active: str = "", subtitle: str = "") -> Iterator[ui.column]:
    """Общий каркас: тёмная тема, шапка с индикаторами, меню и контент-колонка."""
    apply_theme()
    settings = get_settings()

    drawer = ui.left_drawer(value=True, bordered=False).classes("y-drawer p-0").props("width=268 breakpoint=800")
    with drawer:
        with ui.row().classes("items-center gap-3 px-4 py-4 w-full no-wrap"):
            with ui.element("div").classes("y-brand-mark"):
                ui.icon("verified_user").classes("text-white text-[20px]")
            ui.label("Y360 Admin").classes("font-bold text-lg y-gradient-text truncate")
        ui.separator().classes("opacity-10")
        with ui.list().props("paddingless").classes("w-full py-2"):
            for group, items in NAV_GROUPS:
                ui.item_label(group).classes("y-kv-label px-4 pt-4 pb-1")
                for label, target, icon in items:
                    _nav_item(label, target, icon, active)
        with ui.column().classes("absolute bottom-0 left-0 right-0 gap-1 px-4 py-3"):
            ui.separator().classes("opacity-10 mb-2")
            ui.label(f"PostgreSQL · {settings.pg_database}").classes("text-[11px] text-slate-500")

    with ui.header(elevated=False).classes("y-header items-center gap-3 px-4 h-16"):
        ui.button(icon="menu", on_click=drawer.toggle).props("flat round dense color=white")
        with ui.column().classes("gap-0 leading-tight min-w-0"):
            ui.label(title).classes("text-lg font-semibold truncate")
            if subtitle:
                ui.label(subtitle).classes("text-xs text-slate-400 truncate")
        ui.space()
        with ui.row().classes("gap-2 items-center hidden md:flex"):
            _env_chip("AD", "мок" if settings.mock_ad else settings.ad_server, settings.mock_ad)
            _env_chip("IMAP", "мок" if settings.mock_imap else settings.imap_host, settings.mock_imap)
            _env_chip("API", "мок" if settings.mock_yandex else "api360", settings.mock_yandex)
        dark = ui.dark_mode()
        ui.button(icon="contrast", on_click=dark.toggle).props("flat round dense color=white")

    with ui.column().classes("w-full max-w-[1500px] mx-auto p-4 md:p-6 gap-4") as content:
        yield content
