"""Кого не берём в очередь архивации.

Сервисные (роботные) ящики Яндекс 360 — не почта сотрудника: у них нет
владельца, IMAP для них закрыт («forbidden account type»), поэтому снимок
технически невозможен. Такие ящики не должны попадать в очередь архивации и
показываться как «ожидают снимка» — иначе администратор видит вечные ошибки.

Признаки исключения:
  * isRobot в карточке Яндекс 360;
  * логин перечислен в ARCHIVE_EXCLUDE_LOGINS (.env, через запятую) —
    для служебных ящиков, которые роботами не помечены.
"""
from __future__ import annotations

from core.config import get_settings


def excluded_logins() -> set[str]:
    """Логины из ARCHIVE_EXCLUDE_LOGINS (в нижнем регистре)."""
    raw = get_settings().archive_exclude_logins or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def is_excluded_login(login: str | None) -> bool:
    return bool(login) and str(login).strip().lower() in excluded_logins()


def is_archivable(user) -> tuple[bool, str]:
    """Можно ли ставить ящик в очередь архивации: (можно, причина отказа).

    Принимает объект YandexUser или словарь из Directory API.
    """
    if isinstance(user, dict):
        login = user.get("nickname") or (user.get("email") or "").split("@")[0] or ""
        is_robot = bool(user.get("isRobot") or user.get("is_robot"))
        status = str(user.get("status") or "").lower()
    else:
        login = getattr(user, "nickname", None) or getattr(user, "login", None) or ""
        is_robot = bool(getattr(user, "is_robot", False))
        status = str(getattr(user, "status", "") or "").lower()

    if is_robot:
        return False, "сервисный ящик (робот)"
    if status in ("deleted",) and not login:
        return False, "учётка без логина"
    if is_excluded_login(login):
        return False, "логин в ARCHIVE_EXCLUDE_LOGINS"
    return True, ""


def split_archivable(users) -> tuple[list, list[tuple[str, str]]]:
    """Делит список на подлежащих архивации и исключённых: (allowed, [(login, причина)])."""
    allowed: list = []
    skipped: list[tuple[str, str]] = []
    for user in users:
        ok, reason = is_archivable(user)
        if ok:
            allowed.append(user)
        else:
            login = (user.get("nickname") if isinstance(user, dict) else getattr(user, "nickname", "")) or "?"
            skipped.append((str(login), reason))
    return allowed, skipped
