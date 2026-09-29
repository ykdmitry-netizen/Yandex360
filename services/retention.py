"""Правила «срок хранения ящика до удаления» в зависимости от должности (ТЗ п.3).

Правила хранятся в таблице retention_rules и редактируются через UI — реестр
категорий меняется без правки кода. Должность сотрудника нормализуется
(регистр, ё->е, лишние пробелы) и сравнивается с ключевыми словами; выигрывает
правило с наименьшим priority. Короткие слова (ИТ, IT) ищутся как целое слово,
чтобы «активист» не попал в ИТ-категорию.

Правило, подобранное при снимке, фиксируется в dismissals.retention_rule вместе
с должностью: перенастройка реестра не меняет задним числом уже назначенные сроки.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from core.db import execute, execute_returning, query_all, query_one
from core.logs import get_logger

logger = get_logger(__name__)

UNITS = ("days", "months")
_UNIT_LABEL = {"days": "дн.", "months": "мес."}


@dataclass
class RetentionRule:
    id: int
    name: str
    keywords: str
    delete_after_amount: int
    delete_after_unit: str
    priority: int
    active: bool
    notes: str | None = None
    # Заполняется, когда правило взято из официального перечня должностей
    exact_position: str | None = None

    @property
    def is_fallback(self) -> bool:
        return not parse_keywords(self.keywords)

    @property
    def label(self) -> str:
        return f"{self.delete_after_amount} {_UNIT_LABEL.get(self.delete_after_unit, self.delete_after_unit)}"

    @staticmethod
    def from_row(row: dict) -> "RetentionRule":
        return RetentionRule(
            id=int(row["id"]),
            name=row["name"],
            keywords=row.get("keywords") or "",
            delete_after_amount=int(row["delete_after_amount"]),
            delete_after_unit=row.get("delete_after_unit") or "days",
            priority=int(row["priority"]),
            active=bool(row["active"]),
            notes=row.get("notes"),
        )


def normalize(text: str | None) -> str:
    """Нормализация строки для сравнения должностей с ключевыми словами."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text).replace("ё", "е")).strip().lower()


def parse_keywords(raw: str | None) -> list[str]:
    """«а, б, в» -> ['а', 'б', 'в'] (пустые токены отбрасываются)."""
    return [k for k in (normalize(part) for part in (raw or "").split(",")) if k]


def _keyword_matches(position_norm: str, keyword: str) -> bool:
    if len(keyword) <= 3:
        # Короткие аббревиатуры (ИТ, IT) — только как отдельное слово.
        return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", position_norm) is not None
    return keyword in position_norm


def matches(rule: RetentionRule, position: str | None) -> bool:
    if rule.is_fallback:
        return False
    position_norm = normalize(position)
    if not position_norm:
        return False
    return any(_keyword_matches(position_norm, kw) for kw in parse_keywords(rule.keywords))


def load_rules(active_only: bool = True) -> list[RetentionRule]:
    sql = "SELECT * FROM retention_rules"
    if active_only:
        sql += " WHERE active"
    sql += " ORDER BY priority ASC, id ASC"
    return [RetentionRule.from_row(r) for r in query_all(sql)]


def get_rule(rule_id: int) -> RetentionRule | None:
    row = query_one("SELECT * FROM retention_rules WHERE id=%s", (rule_id,))
    return RetentionRule.from_row(row) if row else None


def load_exact_positions() -> dict[str, RetentionRule]:
    """Официальный перечень должностей: нормализованная должность -> правило.

    Таблица retention_positions заполняется из db/retention_positions.txt и имеет
    приоритет над подбором по ключевым словам: в документе «Директор шахты» — 2 мес.,
    а «Директор по производству» — 3 мес., и путать их нельзя.
    """
    try:
        rows = query_all("SELECT position, position_norm, months, days, rule_name "
                         "FROM retention_positions")
    except Exception as exc:  # noqa: BLE001 — до загрузки перечня таблицы может не быть
        logger.warning("Официальный перечень должностей недоступен: %s", exc)
        return {}
    result: dict[str, RetentionRule] = {}
    for row in rows:
        months, days = row.get("months"), row.get("days")
        if months:
            amount, unit = int(months), "months"
        elif days:
            amount, unit = int(days), "days"
        else:
            continue
        result[row["position_norm"]] = RetentionRule(
            id=-1, name=row.get("rule_name") or "Официальный перечень", keywords="",
            delete_after_amount=amount, delete_after_unit=unit, priority=0, active=True,
            notes=f"официальный перечень: {row['position']}",
            exact_position=row["position"])
    return result


def resolve_rule(position: str | None, rules: Iterable[RetentionRule] | None = None) -> RetentionRule | None:
    """Подбирает правило: официальный перечень → ключевые слова → fallback."""
    position_norm = normalize(position)
    if position_norm:
        exact = load_exact_positions()
        if position_norm in exact:
            return exact[position_norm]
    ordered = list(rules) if rules is not None else load_rules()
    for rule in ordered:
        if not rule.active:
            continue
        if matches(rule, position):
            return rule
    return next((r for r in ordered if r.active and r.is_fallback), None)


def add_period(base: datetime, amount: int, unit: str) -> datetime:
    """Прибавляет дни или месяцы (месяцы — с ограничением по длине месяца)."""
    if unit == "months":
        total = base.month - 1 + amount
        year, month = base.year + total // 12, total % 12 + 1
        day = min(base.day, _days_in_month(year, month))
        return base.replace(year=year, month=month, day=day)
    return base + timedelta(days=amount)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    from calendar import monthrange

    return monthrange(year, month)[1]


def plan_deletion(
    position: str | None,
    *,
    backup_done_at: datetime | None = None,
    rules: Iterable[RetentionRule] | None = None,
) -> dict:
    """Возвращает план удаления ящика: {'rule','deletion_at','amount','unit','matched_by'}.

    rule=None означает, что правил в реестре нет вообще — вызывающий обязан
    откатиться на прежнее глобальное поведение, а не молча удалить ящик раньше срока.
    """
    ordered = list(rules) if rules is not None else load_rules()
    rule = resolve_rule(position, ordered)
    base = backup_done_at or datetime.now(timezone.utc)
    if rule is None:
        logger.warning("Правило хранения для должности %r не найдено — реестр пуст", position)
        return {"rule": None, "deletion_at": None, "amount": None, "unit": None, "matched_by": None}
    deletion_at = add_period(base, rule.delete_after_amount, rule.delete_after_unit)
    if rule.exact_position:
        matched = [f"точная должность: {rule.exact_position}"]
    else:
        matched = [kw for kw in parse_keywords(rule.keywords) if _keyword_matches(normalize(position), kw)]
    logger.info(
        "Срок хранения по должности %r: правило «%s» (%s), удаление %s",
        position, rule.name, rule.label, deletion_at.date(),
    )
    return {
        "rule": rule,
        "deletion_at": deletion_at,
        "amount": rule.delete_after_amount,
        "unit": rule.delete_after_unit,
        "matched_by": ", ".join(matched) if matched else "правило по умолчанию",
    }


# ---------- CRUD для UI ----------

def save_rule(
    *,
    name: str,
    keywords: str,
    delete_after_amount: int,
    delete_after_unit: str = "days",
    priority: int = 100,
    active: bool = True,
    notes: str | None = None,
    rule_id: int | None = None,
) -> int:
    """Создаёт или обновляет правило. Возвращает id."""
    if delete_after_unit not in UNITS:
        raise ValueError(f"Неизвестная единица хранения: {delete_after_unit}")
    if delete_after_amount <= 0:
        raise ValueError("Срок хранения должен быть больше нуля")
    if rule_id is None:
        row = query_one("SELECT id FROM retention_rules WHERE name=%s", (name,))
        rule_id = int(row["id"]) if row else None
    if rule_id is None:
        return int(
            execute_returning(
                """
                INSERT INTO retention_rules
                    (name, keywords, delete_after_amount, delete_after_unit, priority, active, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """,
                (name, keywords, delete_after_amount, delete_after_unit, priority, active, notes),
            )
        )
    execute(
        """
        UPDATE retention_rules SET name=%s, keywords=%s, delete_after_amount=%s,
               delete_after_unit=%s, priority=%s, active=%s, notes=%s
        WHERE id=%s
        """,
        (name, keywords, delete_after_amount, delete_after_unit, priority, active, notes, rule_id),
    )
    logger.info("Правило хранения #%s «%s» обновлено (%s)", rule_id, name, delete_after_amount)
    return rule_id


def set_active(rule_id: int, active: bool) -> None:
    execute("UPDATE retention_rules SET active=%s WHERE id=%s", (active, rule_id))
    logger.info("Правило #%s: active=%s", rule_id, active)


def delete_rule(rule_id: int) -> None:
    execute("DELETE FROM retention_rules WHERE id=%s", (rule_id,))
    logger.info("Правило #%s удалено", rule_id)


def preview(position: str | None) -> dict:
    """Для UI: что получит сотрудник с такой должностью прямо сейчас."""
    plan = plan_deletion(position)
    rule = plan["rule"]
    return {
        "rule_name": rule.name if rule else "— нет активных правил —",
        "label": rule.label if rule else "—",
        "deletion_at": plan["deletion_at"],
        "matched_by": plan["matched_by"],
    }
