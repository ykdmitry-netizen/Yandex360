"""Визуальная тема консоли: тёмная палитра, карточки, бейджи, таблицы.

Стили заданы CSS-классами (y-*), чтобы страницы оставались декларативными,
а внешний вид менялся в одном месте.
"""
from __future__ import annotations

from nicegui import ui

# Палитра акцентов
ACCENT = "#6366f1"
ACCENT_2 = "#8b5cf6"

# Статусы жизненного цикла -> (подпись, вид бейджа, иконка)
STATUS_META: dict[str, tuple[str, str, str]] = {
    "new": ("Новый", "info", "fiber_new"),
    "detected": ("Обнаружен", "warning", "search"),
    "accepted": ("Принят в архивацию", "accent", "move_to_inbox"),
    "backed_up": ("Снимок сделан", "success", "photo_camera"),
    "delete_scheduled": ("Удаление запланировано", "violet", "event"),
    "deleted": ("Ящик удалён", "neutral", "delete_forever"),
    "dismissed": ("Вернулся в AD", "success", "undo"),
    "running": ("Выполняется", "info", "sync"),
    "success": ("Успех", "success", "check_circle"),
    "error": ("Ошибка", "danger", "error"),
    "skipped": ("Пропущено", "neutral", "redo"),
}

RUN_REASON_LABELS = {
    "manual": "Вручную",
    "scheduled": "По расписанию",
    "dismissal": "Увольнение",
    "pre_delete": "Перед удалением",
    "restore_mail": "Возврат в ящик",
    "restore_preview": "Предпросмотр возврата",
}

_CSS = f"""
:root {{ --y-accent: {ACCENT}; --y-accent-2: {ACCENT_2}; }}
body.body--dark {{ background: #0b1120 !important; color: #e2e8f0; }}
.body--dark .q-drawer, .body--dark .q-header {{ color: #e2e8f0; }}

/* Вкладки без серой подложки: Quasar красит q-tab-panels в тёмно-серый (#121212) */
.q-tab-panels, .q-tab-panels .q-panel, .q-tab-panel {{ background: transparent !important; }}
/* Боковое меню: никаких горизонтальных полос прокрутки */
.q-drawer, .q-drawer__content {{ overflow-x: hidden !important; }}

.y-header {{ background: rgba(15, 23, 42, .88) !important; backdrop-filter: blur(10px);
             border-bottom: 1px solid rgba(148,163,184,.12); }}
.y-drawer {{ background: #0f172a !important; border-right: 1px solid rgba(148,163,184,.1); }}

.y-brand-mark {{ width: 34px; height: 34px; border-radius: 10px; display: flex; align-items: center;
                 justify-content: center; background: linear-gradient(135deg, {ACCENT}, {ACCENT_2});
                 box-shadow: 0 4px 14px rgba(99,102,241,.4); }}

.y-nav {{ border-radius: 10px; margin: 2px 8px; color: #94a3b8; cursor: pointer;
          transition: background .15s ease, color .15s ease; min-height: 38px; }}
.y-nav:hover {{ background: rgba(99,102,241,.12); color: #c7d2fe; }}
.y-nav-active {{ background: linear-gradient(90deg, rgba(99,102,241,.24), rgba(139,92,246,.10));
                 color: #e0e7ff !important; }}
.y-nav-active .q-icon {{ color: #a5b4fc !important; }}

.y-card {{ background: rgba(17,24,39,.72) !important; border: 1px solid rgba(148,163,184,.12) !important;
           border-radius: 16px !important; box-shadow: 0 10px 30px rgba(2,6,23,.28); }}
.y-card:hover {{ border-color: rgba(99,102,241,.35) !important; }}

.y-stat-icon {{ width: 44px; height: 44px; border-radius: 12px; display: flex;
                align-items: center; justify-content: center; flex: none; }}
.y-tone-indigo  {{ background: rgba(99,102,241,.16);  color: #a5b4fc; }}
.y-tone-violet  {{ background: rgba(139,92,246,.16);  color: #c4b5fd; }}
.y-tone-sky     {{ background: rgba(56,189,248,.14);  color: #7dd3fc; }}
.y-tone-emerald {{ background: rgba(16,185,129,.14);  color: #6ee7b7; }}
.y-tone-amber   {{ background: rgba(245,158,11,.15);  color: #fcd34d; }}
.y-tone-rose    {{ background: rgba(244,63,94,.15);   color: #fda4af; }}
.y-tone-slate   {{ background: rgba(148,163,184,.15); color: #cbd5e1; }}

.y-badge {{ padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
            line-height: 20px; display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }}
.y-badge-neutral {{ background: rgba(148,163,184,.15); color: #cbd5e1; }}
.y-badge-info    {{ background: rgba(56,189,248,.15);  color: #7dd3fc; }}
.y-badge-success {{ background: rgba(16,185,129,.16);  color: #6ee7b7; }}
.y-badge-warning {{ background: rgba(245,158,11,.16);  color: #fcd34d; }}
.y-badge-danger  {{ background: rgba(244,63,94,.16);   color: #fda4af; }}
.y-badge-accent  {{ background: rgba(99,102,241,.18);  color: #c7d2fe; }}
.y-badge-violet  {{ background: rgba(139,92,246,.18);  color: #ddd6fe; }}

.y-table .q-table thead tr {{ background: rgba(15,23,42,.65); }}
.y-table .q-table thead th {{ color: #94a3b8; font-size: 11px; text-transform: uppercase;
                              letter-spacing: .05em; font-weight: 600; }}
.y-table .q-table tbody td {{ font-size: 13px; }}
.y-table .q-table tbody tr:hover {{ background: rgba(99,102,241,.06); }}

.y-section-title {{ font-weight: 600; font-size: 15px; color: #e2e8f0; }}
.y-kv-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: #64748b; }}
.y-gradient-text {{ background: linear-gradient(135deg, #a5b4fc, #8b5cf6);
                    -webkit-background-clip: text; background-clip: text; color: transparent; }}
.y-mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
.y-log .q-log, .y-log {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }}
.y-scroll-x {{ overflow-x: auto; }}
"""


def apply_theme() -> None:
    """Подключает стили темы и тёмный режим (вызывается на каждой странице)."""
    ui.add_css(_CSS)
    ui.dark_mode(True)
    ui.colors(primary=ACCENT, secondary=ACCENT_2)
