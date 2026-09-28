"""Восстановление: просмотр снимка, экспорт писем и возврат в ящик по IMAP."""
from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

from nicegui import ui

from core.config import get_settings
from services import imap_restore, restore
from services.storage import get_storage
from web.components import (
    badge,
    data_table,
    empty_state,
    fmt_dt,
    fmt_size,
    kv,
    run_task,
    section_title,
)
from web.layout import warn_banner

_MAX_MESSAGES = 800  # больше не нужно для UI-таблицы, полный список остаётся в снимке


def render() -> None:
    cfg = get_settings()
    storage = get_storage()
    state: dict = {"login": None, "stamp": None, "snapshot": None, "mbox": None,
                   "entries": [], "folders": [], "manifest": {}, "error": None}

    def logins_with_backups() -> list[str]:
        org_dir = storage.root / cfg.yandex_org_id
        if not org_dir.exists():
            return []
        return sorted(p.name for p in org_dir.iterdir() if p.is_dir())

    def stamps_for(login: str) -> list[str]:
        return [p.name for p in sorted(storage.list_backups(cfg.yandex_org_id, login), reverse=True)]

    async def load_snapshot() -> None:
        login = state.get("login")
        if not login:
            ui.notify("Сначала выберите сотрудника", type="warning")
            return

        def worker(log):
            log(f"Ищу снимок {login}…")
            snapshot = restore.find_snapshot(storage, cfg.yandex_org_id, login, state.get("stamp"))
            log(f"Снимок: {snapshot.name}")
            work_dir = Path(tempfile.gettempdir()) / "y360-restore-extract"
            mbox = restore.ensure_mbox(snapshot, work_dir)
            log(f"MBOX: {mbox.name}")
            entries = restore.read_index(snapshot, mbox)
            log(f"Писем в снимке: {len(entries)}")
            folders = restore.folder_summary(entries)
            log(f"Папок: {len(folders)}")
            log("Проверяю целостность (sha256)…")
            verified = storage.verify_snapshot(snapshot)
            log("Целостность подтверждена" if verified else "Внимание: sha256 не совпадает с manifest", "info" if verified else "warning")
            return {
                "snapshot": snapshot, "mbox": mbox, "entries": entries,
                "folders": folders, "manifest": storage.read_manifest(snapshot) or {},
            }

        result = await run_task(
            "Загрузка снимка",
            worker,
            description="Читает индекс писем, распаковывает архив при необходимости и проверяет sha256.",
        )
        if result is None:
            return
        state.update(result)
        state["error"] = None
        snapshot_section.refresh()

    @ui.refreshable
    def snapshot_section() -> None:
        if state["error"]:
            warn_banner(state["error"])
        if not state["snapshot"]:
            empty_state("Выберите сотрудника и снимок, затем нажмите «Загрузить снимок»", "folder_open")
            return

        manifest = state["manifest"] or {}
        with ui.card().classes("y-card p-4 gap-3 w-full"):
            section_title("Снимок", str(state["snapshot"]), "folder_open")
            with ui.row().classes("gap-8 flex-wrap"):
                kv("Дата снимка", fmt_dt(manifest.get("created_at")))
                kv("Писем", manifest.get("messages", "—"))
                kv("Размер", fmt_size(manifest.get("size_bytes")))
                kv("Папок", manifest.get("folders_count", "—"))
                kv("Причина", manifest.get("reason") or "—")
                kv("sha256", (manifest.get("archive_sha256") or "—")[:16] + "…", mono=True)

        with ui.row().classes("gap-2 flex-wrap"):
            badge(f"Писем: {len(state['entries'])}", "accent", "mail")
            badge(f"Папок: {len(state['folders'])}", "info", "folder")

        with ui.card().classes("y-card p-4 gap-2 w-full"):
            section_title("Папки", "сколько писем в каждой", "folder_copy")
            if not state["folders"]:
                empty_state("Список папок пуст", "folder_off")
            else:
                data_table(
                    [
                        {"name": "folder", "label": "Папка", "field": "folder", "align": "left", "sortable": True},
                        {"name": "messages", "label": "Писем", "field": "messages", "align": "right", "sortable": True},
                    ],
                    state["folders"],
                    search=False,
                    rows_per_page=8,
                    row_key="folder",
                )

        entries = state["entries"][:_MAX_MESSAGES]
        rows = [
            {
                "folder": e.get("folder") or "—",
                "uid": str(e.get("uid") or "—"),
                "subject": e.get("subject") or "(без темы)",
                "from": e.get("from") or "—",
                "date": e.get("date") or "—",
                # скрытые поля для экспорта/восстановления
                "offset": e.get("offset"),
                "length": e.get("length"),
                "message_id": e.get("message_id"),
            }
            for e in entries
        ]
        table = data_table(
            [
                {"name": "folder", "label": "Папка", "field": "folder", "align": "left", "sortable": True},
                {"name": "uid", "label": "UID", "field": "uid", "align": "left"},
                {"name": "subject", "label": "Тема", "field": "subject", "align": "left", "sortable": True},
                {"name": "from", "label": "От кого", "field": "from", "align": "left"},
                {"name": "date", "label": "Дата письма", "field": "date", "align": "left"},
            ],
            rows,
            title=f"Письма (первые {len(rows)} из {len(state['entries'])})",
            subtitle="отметьте строки галочками — затем экспорт или возврат в ящик",
            rows_per_page=20,
            selection="multiple",
            row_key="offset",
        )

        def selected_entries() -> list[dict]:
            selected = table.selected or []
            return [dict(r) for r in selected]

        def download_eml_zip() -> None:
            selected = selected_entries()
            if not selected:
                ui.notify("Отметьте хотя бы одно письмо", type="warning")
                return
            mbox = Path(state["mbox"])
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for index, entry in enumerate(selected):
                    archive.writestr(restore.eml_name(entry, index), restore.read_message(mbox, entry))
            ui.download(buffer.getvalue(), f"{state['login']}_letters.zip", "application/zip")

        def download_mbox() -> None:
            selected = selected_entries()
            if not selected:
                ui.notify("Отметьте хотя бы одно письмо", type="warning")
                return
            mbox = Path(state["mbox"])
            parts = []
            for entry in selected:
                record = restore.read_record(mbox, entry)
                parts.append(record if record.endswith(b"\n") else record + b"\n")
            ui.download(b"".join(parts), f"{state['login']}_letters.mbox", "application/mbox")

        targets = imap_restore.list_targets(cfg.yandex_org_id)
        options = {t["email"]: t["label"] for t in targets}

        async def restore_to_mailbox() -> None:
            selected = selected_entries()
            if not selected:
                ui.notify("Отметьте хотя бы одно письмо", type="warning")
                return
            keys, message_ids = [], []
            for entry in selected:
                if entry.get("uid"):
                    keys.append((entry.get("folder") or "", str(entry["uid"])))
                elif entry.get("message_id"):
                    message_ids.append(entry["message_id"])
            target = target_select.value
            is_dry = bool(dry_switch.value)

            def worker(log):
                return imap_restore.restore_to_mailbox(
                    cfg.yandex_org_id, state["login"], target,
                    stamp=state.get("stamp"),
                    keys=keys or None,
                    message_ids=message_ids or None,
                    dry_run=is_dry,
                    log_func=log,
                )

            result = await run_task(
                "Возврат писем в ящик" + (" (проверка)" if is_dry else ""),
                worker,
                description="Письма лягут в папку «Восстановлено/<логин>/<папка>»; дубликаты по Message-ID пропускаются.",
            )
            if result:
                ui.notify(
                    f"План: {result.get('planned', 0)}; добавлено: {result.get('appended', 0)}; "
                    f"пропущено: {result.get('skipped', 0)}; ошибок: {result.get('failed', 0)}",
                    type="positive" if not result.get("failed") else "warning",
                    multi_line=True,
                )

        with ui.card().classes("y-card p-4 gap-3 w-full"):
            section_title("Действия с выбранными письмами", "экспорт или возврат в почтовый ящик", "move_to_inbox")
            with ui.row().classes("items-center gap-3 flex-wrap"):
                ui.button("Скачать .eml (zip)", icon="folder_zip", on_click=download_eml_zip).props("outline")
                ui.button("Скачать .mbox", icon="mail", on_click=download_mbox).props("outline")
            ui.separator().classes("opacity-10")
            with ui.row().classes("items-center gap-3 flex-wrap"):
                if options:
                    target_select = ui.select(options, label="Ящик-получатель", value=next(iter(options))).classes(
                        "w-96"
                    ).props("outlined dense")
                else:
                    target_select = ui.select({}, label="Ящик-получатель").classes("w-96").props("outlined dense")
                    ui.label("Нет доступных ящиков-получателей").classes("text-xs text-amber-300")
                dry_switch = ui.switch("Только проверка", value=True)
                ui.button("Вернуть в ящик", icon="unarchive", on_click=restore_to_mailbox).props("unelevated color=primary")

    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
        section_title("Восстановление", "просмотр снимков и возврат почты", "unarchive")
        ui.button("Обновить", icon="refresh", on_click=snapshot_section.refresh).props("flat")

    logins = logins_with_backups()
    with ui.card().classes("y-card p-4 gap-3 w-full"):
        section_title("Выбор снимка", "снимки хранятся в data/backups", "search")
        with ui.row().classes("items-end gap-3 flex-wrap"):
            login_select = ui.select(logins, label="Сотрудник (логин)", with_input=True).classes("w-72").props(
                "outlined dense"
            )
            stamp_select = ui.select([], label="Снимок (метка)").classes("w-64").props("outlined dense")

            def on_login_change() -> None:
                login = login_select.value
                state["login"] = login
                state["snapshot"] = None
                stamps = stamps_for(login) if login else []
                stamp_select.options = stamps
                stamp_select.value = stamps[0] if stamps else None
                stamp_select.update()
                snapshot_section.refresh()

            login_select.on_value_change(on_login_change)
            stamp_select.on_value_change(lambda: state.update(stamp=stamp_select.value))
            ui.button("Загрузить снимок", icon="folder_open", on_click=load_snapshot).props("unelevated color=primary")

        if not logins:
            ui.label("Снимков пока нет — сделайте их в разделе «Архивация»").classes("text-xs text-slate-400")
        else:
            login_select.value = logins[0]
            on_login_change()

    snapshot_section()
