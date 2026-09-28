"""Сервис работы с Directory API: пользователи, подразделения, организации.

Таблица users — справочник для UI и база для жизненного цикла увольнений.
"""
from __future__ import annotations

from typing import List, Optional

from core.config import get_settings
from core.db import execute, query_all, query_one
from core.logs import get_logger
from core.models import Department, User
from integrations.yandex import YandexAPIClient, yandex_client_from_settings

logger = get_logger(__name__)


def _client() -> YandexAPIClient:
    return yandex_client_from_settings()


def _user_name(raw: dict) -> Optional[str]:
    """Имя пользователя из Directory API.

    В API Яндекс 360 поле name — объект {"first": ..., "last": ..., "middle": ...},
    а не строка. Склеиваем в "Фамилия Имя Отчество".
    """
    name = raw.get("name")
    if isinstance(name, dict):
        parts: list[str] = [
            str(name[k]) for k in ("last", "first", "middle") if name.get(k)
        ]
        return " ".join(parts).strip() or None
    return name or None


def sync_users() -> int:
    """Загружает всех пользователей из API в таблицу users, возвращает их число."""
    client = _client()
    raw_users = client.list_users()
    cfg = get_settings()
    count = 0
    for u in raw_users:
        uid = u.get("id")
        if not uid:
            continue
        login = u.get("nickname") or u.get("email", "").split("@")[0] or ""
        # СНИЛС в Яндекс 360 хранится в externalId (строка); массив externalIds
        # встречается в моках и старых выгрузках.
        snils = u.get("externalId") or u.get("external_id") or None
        if not snils:
            for item in u.get("externalIds") or []:
                if isinstance(item, dict) and str(item.get("type", "")).lower() == "snils":
                    snils = item.get("id") or item.get("value")
                    break
        execute(
            """
            INSERT INTO users (id, org_id, login, nickname, name, position, department_id, email, snils, status, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                login=EXCLUDED.login,
                nickname=EXCLUDED.nickname,
                name=EXCLUDED.name,
                position=EXCLUDED.position,
                department_id=EXCLUDED.department_id,
                email=EXCLUDED.email,
                snils=COALESCE(EXCLUDED.snils, users.snils),
                status=EXCLUDED.status,
                updated_at=now()
            """,
            (
                uid,
                cfg.yandex_org_id,
                login,
                u.get("nickname"),
                _user_name(u),
                u.get("position") or None,
                str(u["departmentId"]) if u.get("departmentId") else None,
                u.get("email"),
                snils,
                u.get("status", "active"),
            ),
        )
        count += 1
    logger.info("Синхронизировано пользователей: %s", count)
    return count


def list_users(org_id: Optional[str] = None) -> List[User]:
    cfg = get_settings()
    oid = org_id or cfg.yandex_org_id
    rows = query_all(
        "SELECT id, org_id, login, nickname, name, position, department_id, email, snils, status "
        "FROM users WHERE org_id=%s ORDER BY name NULLS LAST, login",
        (oid,),
    )
    return [User(**r) for r in rows]


def find_user(login: str) -> Optional[User]:
    row = query_one(
        "SELECT id, org_id, login, nickname, name, position, department_id, email, snils, status "
        "FROM users WHERE login=%s LIMIT 1",
        (login,),
    )
    return User(**row) if row else None


def upsert_user(user: User) -> None:
    execute(
        """
        INSERT INTO users (id, org_id, login, nickname, name, position, department_id, email, status, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
        ON CONFLICT (id) DO UPDATE SET
            login=EXCLUDED.login, name=EXCLUDED.name, position=EXCLUDED.position,
            department_id=EXCLUDED.department_id,
            email=EXCLUDED.email, status=EXCLUDED.status, updated_at=now()
        """,
        (user.id, user.org_id, user.login, user.nickname, user.name,
         user.position, user.department_id, user.email, user.status),
    )


def list_departments() -> List[Department]:
    client = _client()
    raw = client.list_departments()
    return [
        Department(
            id=str(d["id"]),
            name=d.get("name", ""),
            parent_id=str(d["parentId"]) if d.get("parentId") else None,
        )
        for d in raw
    ]


def get_org() -> dict:
    return _client().get_org()
