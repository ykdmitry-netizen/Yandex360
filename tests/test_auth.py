"""Проверки авторизации консоли (web/auth.py).

Что проверяется:
  * режим none не включает защиту (поведение демо-стенда не меняется);
  * список публичных путей: вход и статика открыты, страницы и WebSocket — нет;
  * кука сессии подписывается и проверяется, подделка отбивается;
  * разбор групп из claim (строка, список, DOMAIN\\Group) и решение о доступе;
  * проверка id_token: подпись RS256, iss, aud, exp, nonce — на локально
    сгенерированном ключе (боевой ADFS для теста не нужен);
  * ASGI-защита: страница без сессии → редирект на /login, WebSocket → закрыт.

Запуск: <repo>/venv/bin/python tests/test_auth.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((bool(cond), name, detail))
    print(f"[{' OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def set_env(**values) -> None:
    """Меняет .env-значения и сбрасывает закэшированные настройки."""
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    import core.config as config

    config._settings = None  # noqa: SLF001 — принудительная перезагрузка настроек


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_token(private_key, *, iss: str, aud: str, nonce: str = "", exp_offset: int = 600,
               kid: str = "test-key", claims_extra: dict | None = None) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    payload = {"iss": iss, "aud": aud, "sub": "u1", "upn": "ivanov@kolmar.ru",
               "name": "Иванов Иван", "exp": int(time.time()) + exp_offset}
    if nonce:
        payload["nonce"] = nonce
    payload.update(claims_extra or {})
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}"
    signature = private_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{b64url(signature)}"


def test_none_mode() -> None:
    set_env(AUTH_MODE="none")
    from web import auth

    check("AUTH_MODE=none: защита выключена", auth.get_settings().auth_enabled is False)
    check("AUTH_MODE=none: страницы доступны (/)", auth.public_path("/") is False
          and auth.get_settings().auth_enabled is False)
    try:
        auth.install(object())  # app не понадобится: режим none выходит сразу
        check("AUTH_MODE=none: install() не требует приложения", True)
    except Exception as exc:  # noqa: BLE001
        check("AUTH_MODE=none: install() не требует приложения", False, str(exc))


def test_public_paths() -> None:
    set_env(AUTH_MODE="oidc", SESSION_SECRET="x" * 40, OIDC_ISSUER="https://sso.kolmar.ru/adfs",
            OIDC_CLIENT_ID="cid", OIDC_CLIENT_SECRET="secret",
            OIDC_REDIRECT_URI="https://y360.kolmar.ru/oauth2/callback")
    from web import auth

    for path in ("/login", "/oauth2/callback", "/logout", "/auth/me",
                 "/_nicegui/2.24.2/static/nicegui.js", "/favicon.ico"):
        check(f"Открыт без сессии: {path}", auth.public_path(path) is True)
    for path in ("/", "/users", "/backup", "/settings", "/_nicegui_ws/", "/_nicegui_ws/socket.io/"):
        check(f"Защищён сессией: {path}", auth.public_path(path) is False)


def test_session_cookie() -> None:
    from web import auth

    raw = auth.make_session_cookie({"login": "ivanov", "name": "Иванов", "groups": ["G"]})
    data = auth.session_from_cookie(raw)
    check("Кука сессии подписывается и читается",
          bool(data) and data.get("login") == "ivanov" and data.get("groups") == ["G"])
    check("Подделанная кука отбивается",
          auth.session_from_cookie(raw + "x") is None and auth.session_from_cookie("garbage") is None)
    check("Пустая кука — нет сессии", auth.session_from_cookie(None) is None)


def test_group_decision() -> None:
    from web import auth

    set_env(OIDC_ADMIN_GROUP="")
    allowed, _ = auth.access_decision({"upn": "u@kolmar.ru"})
    check("Пустая группа — пускаем любого аутентифицированного", allowed is True)

    set_env(OIDC_ADMIN_GROUP="GRP-Y360-Admins", OIDC_GROUP_CLAIM="group")
    cases = [
        ("список", {"group": ["GRP-Y360-Admins", "Domain Users"]}, True),
        ("строка", {"group": "Domain Users, GRP-Y360-Admins"}, True),
        ("DOMAIN\\группа", {"group": "KOLMAR\\GRP-Y360-Admins"}, True),
        # ADFS нередко отдаёт группы по полному URI Microsoft — распознаём по последнему сегменту
        ("URI-claim Microsoft", {"http://schemas.microsoft.com/ws/2008/06/identity/claims/groups": ["GRP-Y360-Admins"]}, True),
        ("чужая группа", {"group": ["Domain Users"]}, False),
        ("группы нет вовсе", {"upn": "u@kolmar.ru"}, False),
    ]
    for label, claims, expected in cases:
        allowed, reason = auth.access_decision(claims)
        check(f"Решение о доступе ({label})", allowed is expected, reason)

    check("claim_groups: строка разбирается в список",
          auth.claim_groups({"group": "A, B"}) == ["A", "B"])


def test_id_token() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    from web import auth

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    jwk = {"kty": "RSA", "kid": "test-key", "alg": "RS256",
           "n": b64url(numbers.n.to_bytes(256, "big")),
           "e": b64url(numbers.e.to_bytes(3, "big"))}
    auth._jwks = {"keys": [jwk]}      # noqa: SLF001 — подменяем ключи IdP
    auth._jwks_at = time.time()       # noqa: SLF001

    issuer, client, nonce = "https://sso.kolmar.ru/adfs", "cid", "nonce-1"
    good = make_token(key, iss=issuer, aud=client, nonce=nonce,
                      claims_extra={"group": ["GRP-Y360-Admins"]})
    claims = auth.verify_id_token(good, nonce)
    check("id_token: корректный токен принят",
          claims.get("upn") == "ivanov@kolmar.ru" and claims.get("group") == ["GRP-Y360-Admins"])

    for label, token, expected_nonce in (
        ("неверный iss", make_token(key, iss="https://evil.local/adfs", aud=client, nonce=nonce), nonce),
        ("неверный aud", make_token(key, iss=issuer, aud="other-client", nonce=nonce), nonce),
        ("просроченный", make_token(key, iss=issuer, aud=client, nonce=nonce, exp_offset=-3600), nonce),
        ("чужой nonce", make_token(key, iss=issuer, aud=client, nonce="другой"), nonce),
    ):
        try:
            auth.verify_id_token(token, expected_nonce)
            check(f"id_token: отбит ({label})", False, "исключения не было")
        except Exception as exc:  # noqa: BLE001
            check(f"id_token: отбит ({label})", True, type(exc).__name__)

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fake = make_token(other, iss=issuer, aud=client, nonce=nonce)
    try:
        auth.verify_id_token(fake, nonce)
        check("id_token: подпись чужим ключом отбита", False, "исключения не было")
    except Exception as exc:  # noqa: BLE001
        check("id_token: подпись чужим ключом отбита", True, str(exc)[:60])

    check("Не-JWT отбивается",
          _raises(lambda: auth.verify_id_token("не.токен", "")))


def _raises(func) -> bool:
    try:
        func()
        return False
    except Exception:  # noqa: BLE001
        return True


def test_guard() -> None:
    from web import auth

    async def run(path: str, cookie: str | None, kind: str = "http"):
        messages: list[dict] = []
        passed: list[bool] = []

        async def inner(scope, receive, send):
            passed.append(True)

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            messages.append(message)

        headers = [(b"cookie", f"y360_session={cookie}".encode())] if cookie else []
        scope = {"type": kind, "path": path, "method": "GET", "headers": headers}
        await auth.AuthGuard(inner)(scope, receive, send)
        return passed, messages

    set_env(AUTH_MODE="oidc", SESSION_SECRET="x" * 40)
    passed, messages = asyncio.run(run("/users", None))
    location = next((m["headers"] for m in messages if m["type"] == "http.response.start"), [])
    target = dict(location).get(b"location", b"").decode()
    check("Без сессии страница → редирект на /login",
          not passed and target.startswith("/login?next=/users"), target)

    passed, messages = asyncio.run(run("/", None, kind="websocket"))
    check("Без сессии WebSocket → рукопожатие закрыто",
          not passed and any(m["type"] == "websocket.close" for m in messages))

    valid = auth.make_session_cookie({"login": "ivanov", "name": "Иванов", "groups": []})
    passed, _ = asyncio.run(run("/users", valid))
    check("С валидной сессией страница отдаётся", bool(passed))

    passed, _ = asyncio.run(run("/login", None))
    check("Страница входа доступна без сессии", bool(passed))

    set_env(AUTH_MODE="none")
    passed, _ = asyncio.run(run("/users", None))
    check("AUTH_MODE=none: защита не мешает", bool(passed))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    saved = {k: os.environ.get(k) for k in (
        "AUTH_MODE", "SESSION_SECRET", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET",
        "OIDC_REDIRECT_URI", "OIDC_ADMIN_GROUP", "OIDC_GROUP_CLAIM")}
    try:
        try:
            import cryptography  # noqa: F401
            has_crypto = True
        except ImportError:
            has_crypto = False
        if not has_crypto:
            print("[SKIP] cryptography не установлена — проверки id_token пропущены "
                  "(pip install cryptography)")
        for func in (test_none_mode, test_public_paths, test_session_cookie,
                     test_group_decision, test_guard):
            func()
        if has_crypto:
            test_id_token()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
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
