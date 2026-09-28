"""Точный сбор имён иконок, которые код реально передаёт в UI.

Проверяются только места, где строка попадает в аргумент иконки:
ui.icon("..."), icon="...", badge(..., "..."), section_title(..., "..."),
empty_state(..., "..."), stat_card("...", ...), NAV_GROUPS, STATUS_META.
Запуск: <repo>\\venv\\Scripts\\python.exe tests\\collect_icons.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Корень репозитория: тесты лежат в <repo>/tests
REPO = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "icons.json"

RULES = [
    ("ui.icon()", re.compile(r'ui\.icon\(\s*"([a-z0-9_]+)"'), None),
    ("icon=", re.compile(r'icon\s*=\s*"([a-z0-9_]+)"'), None),
    ("badge()", re.compile(r'badge\([^\n]*,\s*"([a-z0-9_]+)"\s*\)'), None),
    ("section_title()", re.compile(r'section_title\([^\n]*,\s*"([a-z0-9_]+)"\s*\)'), None),
    ("empty_state()", re.compile(r'empty_state\([^\n]*,\s*"([a-z0-9_]+)"\s*\)'), None),
    ("stat_card()", re.compile(r'stat_card\(\s*"([a-z0-9_]+)"'), None),
    # списки иконок лежат только в этих файлах — иначе правило ловит обычные кортежи строк
    ("NAV_GROUPS", re.compile(r'\(\s*"[^"]+"\s*,\s*"[^"]+"\s*,\s*"([a-z0-9_]+)"\s*\)'), "web/layout.py"),
    ("STATUS_META", re.compile(r':\s*\(\s*"[^"]*"\s*,\s*"[^"]*"\s*,\s*"([a-z0-9_]+)"\s*\)'), "web/theme.py"),
]


def main() -> None:
    found: dict[str, list[str]] = {}
    for path in sorted((REPO / "web").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        for rule_name, rx, only_in in RULES:
            if only_in and rel != only_in:
                continue
            for name in rx.findall(text):
                found.setdefault(name, [])
                entry = f"{rel} [{rule_name}]"
                if entry not in found[name]:
                    found[name].append(entry)

    OUT.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"найдено имён иконок: {len(found)} -> {OUT}")
    for name in sorted(found):
        print(f"  {name:<22} {', '.join(found[name])}")


if __name__ == "__main__":
    main()
