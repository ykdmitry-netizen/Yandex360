#!/usr/bin/env python3
"""Загрузка официального перечня сроков хранения по должностям.

Читает db/retention_positions.txt (формат строки: «<срок><TAB><должность>»)
и заполняет таблицу retention_positions, которая имеет приоритет над подбором
правила по ключевым словам.

Идемпотентно: существующие должности обновляются, исчезнувшие из файла —
удаляются. Запуск:

    venv/bin/python scripts/load_retention_positions.py
    venv/bin/python scripts/load_retention_positions.py --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_FILE = ROOT / "db" / "retention_positions.txt"

# «3 месяца» / «2 месяца» / «14 дней» → (amount, unit)
PERIOD_RE = re.compile(r"^\s*(\d+)\s*(месяц\w*|дн\w*|день)\s*$", re.IGNORECASE)


def parse_period(text: str) -> tuple[int, str] | None:
    match = PERIOD_RE.match(text)
    if not match:
        return None
    amount = int(match.group(1))
    word = match.group(2).lower()
    unit = "months" if word.startswith("месяц") else "days"
    return amount, unit


def read_file(path: Path) -> list[tuple[str, int, str]]:
    """Возвращает [(должность, количество, единица)] с проверкой дублей."""
    rows: list[tuple[str, int, str]] = []
    seen: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" not in line:
            raise SystemExit(f"{path.name}:{number}: нет разделителя TAB: {line!r}")
        period_text, position = (part.strip() for part in line.split("\t", 1))
        parsed = parse_period(period_text)
        if not parsed:
            raise SystemExit(f"{path.name}:{number}: непонятный срок {period_text!r}")
        key = position.lower()
        if key in seen:
            print(f"   дубль в файле пропущен: {position!r} (уже был как {seen[key]!r})")
            continue
        seen[key] = position
        rows.append((position, parsed[0], parsed[1]))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--source", default="HR-документ «3 месяца / 2 месяца / 14 дней»")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from core.db import execute, query_all
    from services.retention import normalize

    path = Path(args.file)
    if not path.exists():
        print(f"нет файла {path}")
        return 1
    rows = read_file(path)
    by_period: dict[str, int] = {}
    for _, amount, unit in rows:
        by_period[f"{amount} {unit}"] = by_period.get(f"{amount} {unit}", 0) + 1

    print(f"Прочитано должностей: {len(rows)}")
    for period, count in sorted(by_period.items()):
        print(f"   {period:<14} {count}")

    if args.dry_run:
        print("--dry-run: изменения не записываются")
        return 0

    existing = {r["position_norm"] for r in query_all("SELECT position_norm FROM retention_positions")}
    loaded = 0
    for position, amount, unit in rows:
        norm = normalize(position)
        execute(
            """
            INSERT INTO retention_positions (position, position_norm, rule_name, months, days, source, loaded_at)
            VALUES (%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (position) DO UPDATE SET
                position_norm = EXCLUDED.position_norm,
                rule_name     = EXCLUDED.rule_name,
                months        = EXCLUDED.months,
                days          = EXCLUDED.days,
                source        = EXCLUDED.source,
                loaded_at     = now()
            """,
            (position, norm,
             f"Официальный перечень: {amount} " + ("мес." if unit == "months" else "дн."),
             amount if unit == "months" else None,
             amount if unit == "days" else None,
             args.source),
        )
        loaded += 1

    stale = existing - {normalize(p) for p, _, _ in rows}
    for norm in sorted(stale):
        execute("DELETE FROM retention_positions WHERE position_norm=%s", (norm,))
    if stale:
        print(f"Удалено должностей, которых нет в файле: {len(stale)}")

    total = query_all("SELECT count(*) AS n FROM retention_positions")[0]["n"]
    print(f"Загружено: {loaded}; всего в таблице: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
