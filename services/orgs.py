"""Справочник организаций (data/orgs.json) для планирования отделов.

Файл можно редактировать без изменения кода: он перечитывается при каждом
запуске. Если файла нет — создаётся с известными организациями по умолчанию.
"""
from __future__ import annotations

import json
from typing import Dict, List, Tuple

from core.config import ROOT
from core.logs import get_logger

logger = get_logger(__name__)

DATA_DIR = ROOT / "data"
ORGS_FILE = DATA_DIR / "orgs.json"

_KNOWN_ORGS = [
    'ООО "УК "Колмар"',
    'ООО "СибПроектГрупп"',
    'АО "ГОК "Денисовский"',
    'АО "ГОК "Инаглинский"',
    'ООО "Ремонтно-Производственная База "Колмар"',
    'ООО "Колмар-Тур"',
    'АО "КОЛМАР ГРУП"',
    'ООО "Колмар - Продажи и Логистика"',
    'ООО "Энди-Строй"',
    'ООО Демо',
]
_ORG_MAPPING = {
    'СПГРУПП': 'ООО "СибПроектГрупп"',
    'Колмар УК': 'ООО "УК "Колмар"',
    'ГОК Денисовский': 'АО "ГОК "Денисовский"',
    'ГОК Инаглинский': 'АО "ГОК "Инаглинский"',
    'РПБК': 'ООО "Ремонтно-Производственная База "Колмар"',
    'Колмар-Тур': 'ООО "Колмар-Тур"',
    'КОЛМАР ГРУП': 'АО "КОЛМАР ГРУП"',
    'Колмар - Продажи и Логистика': 'ООО "Колмар - Продажи и Логистика"',
    'Энди-Строй': 'ООО "Энди-Строй"',
    'ООО Демо': 'ООО Демо',
}


def load_orgs_config() -> Tuple[List[str], Dict[str, str]]:
    """Загружает список организаций и маппинг из orgs.json (или дефолты)."""
    if not ORGS_FILE.exists():
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            ORGS_FILE.write_text(
                json.dumps({"known_orgs": _KNOWN_ORGS, "org_mapping": _ORG_MAPPING},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Не удалось создать orgs.json: {e}")
        return list(_KNOWN_ORGS), dict(_ORG_MAPPING)
    try:
        data = json.loads(ORGS_FILE.read_text(encoding="utf-8"))
        known = data.get("known_orgs", _KNOWN_ORGS)
        mapping = data.get("org_mapping", _ORG_MAPPING)
        return known, mapping
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Ошибка загрузки orgs.json: {e}, используем значения по умолчанию")
        return list(_KNOWN_ORGS), dict(_ORG_MAPPING)
