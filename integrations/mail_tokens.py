"""Выпуск временных токенов пользователя через OAuth token-exchange (RFC 8693).

Рабочая схема Яндекса (проверена в проекте ya-backup):
  - subject_token      = email сотрудника (НЕ OAuth-токен),
  - subject_token_type = urn:yandex:params:oauth:token-type:email,
  - scope              = mail:imap_full (или mail:imap_ro), обязателен и
                         должен соответствовать правам приложения.
Полученный access_token используется в IMAP XOAUTH2 (user=<email>).
"""
from __future__ import annotations

import requests

from core.config import get_settings
from core.logs import get_logger

logger = get_logger(__name__)

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_TOKEN_TYPE_EMAIL = "urn:yandex:params:oauth:token-type:email"


class TokenExchangeError(Exception):
    pass


def exchange_token(login: str, email: str | None = None) -> str:
    """Возвращает временный access-токен для ящика сотрудника (IMAP XOAUTH2).

    Параметр login нужен только для осмысленного сообщения об ошибке;
    сам обмен выполняется по email сотрудника.
    """
    cfg = get_settings()
    subject = (email or "").strip().lower()
    if not subject:
        raise TokenExchangeError(f"Нет email у пользователя {login} — обмен невозможен")

    payload = {
        "grant_type": GRANT_TYPE,
        "client_id": cfg.yandex_client_id,
        "client_secret": cfg.yandex_client_secret,
        "subject_token": subject,
        "subject_token_type": SUBJECT_TOKEN_TYPE_EMAIL,
        "scope": cfg.yandex_token_scope,
    }

    resp = requests.post(cfg.yandex_token_url, data=payload, timeout=30)
    if resp.status_code != 200:
        raise TokenExchangeError(
            f"token-exchange не удался ({resp.status_code}): {resp.text[:400]}"
        )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise TokenExchangeError(f"Нет access_token в ответе: {data}")
    logger.debug("Выпущен токен для %s", email)
    return token
