"""Проверки правил исключения из архивации (services/filters.py).

Сервисные (роботные) ящики Яндекс 360 бэкапить нельзя — IMAP для них закрыт,
поэтому они не должны попадать в очередь архивации и в счётчик «ожидают снимка».

Запуск: <repo>/venv/bin/python tests/test_filters.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((bool(cond), name, detail))
    print(f"[{' OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def set_env(value: str) -> None:
    os.environ["ARCHIVE_EXCLUDE_LOGINS"] = value
    import core.config as config

    config._settings = None  # noqa: SLF001


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from core.models import YandexUser
    from services import filters

    saved = os.environ.get("ARCHIVE_EXCLUDE_LOGINS")
    set_env("")
    check("Робот исключается из архивации",
          filters.is_archivable(YandexUser(id="1", nickname="yndx-forms-cnt-robot", is_robot=True))[0] is False)
    check("Обычный сотрудник допускается",
          filters.is_archivable(YandexUser(id="2", nickname="ivanov"))[0] is True)
    check("Пустой список исключений ничего не ломает", filters.excluded_logins() == set())

    set_env("kolmar-id-360, security-store")
    check("Логин из ARCHIVE_EXCLUDE_LOGINS исключается",
          filters.is_archivable(YandexUser(id="3", nickname="kolmar-id-360"))[0] is False)
    check("Сравнение логинов не зависит от регистра",
          filters.is_excluded_login("KOLMAR-ID-360") is True)
    check("Список разбирается с пробелами",
          filters.excluded_logins() == {"kolmar-id-360", "security-store"})

    raw_robot = {"id": "4", "nickname": "robot-1", "isRobot": True}
    raw_human = {"id": "5", "nickname": "petrov", "status": "active"}
    check("Словарь из Directory API с isRobot исключается",
          filters.is_archivable(raw_robot)[0] is False)
    check("Словарь обычного сотрудника допускается",
          filters.is_archivable(raw_human)[0] is True)

    allowed, skipped = filters.split_archivable([raw_human, raw_robot,
                                                 YandexUser(id="6", nickname="kolmar-id-360")])
    check("split_archivable делит списки правильно",
          len(allowed) == 1 and len(skipped) == 2
          and {login for login, _ in skipped} == {"robot-1", "kolmar-id-360"},
          f"allowed={len(allowed)} skipped={[s[0] for s in skipped]}")
    check("split_archivable объясняет причину",
          all(reason for _, reason in skipped), str(skipped))

    if saved is None:
        os.environ.pop("ARCHIVE_EXCLUDE_LOGINS", None)
    else:
        os.environ["ARCHIVE_EXCLUDE_LOGINS"] = saved
    import core.config as config

    config._settings = None  # noqa: SLF001

    failed = [name for ok, name, _ in RESULTS if not ok]
    print("=" * 60)
    print(f"Проверок: {len(RESULTS)}; провалено: {len(failed)}")
    for name in failed:
        print(f"  FAIL: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
