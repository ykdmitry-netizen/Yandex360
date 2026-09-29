"""Аутентификация консоли: единый вход через ADFS (OIDC) или доменная учётка.

Режим задаётся в .env:

    AUTH_MODE=none   — без проверки (локальный стенд, демо, автотесты);
    AUTH_MODE=oidc   — единый вход через ADFS (Authorization Code Flow);
    AUTH_MODE=ldap   — вход логином и паролем домена через LDAP-bind.

Схема работы (oidc): браузер → /login → ADFS → /oauth2/callback → проверка
id_token (подпись по JWKS, iss, aud, exp, nonce) → проверка группы в claim →
подписанная кука сессии → доступ к страницам. Все страницы, кроме служебных,
без валидной сессии перенаправляются на /login, а WebSocket-рукопожатие
NiceGUI без сессии закрывается: страницу NiceGUI собирает именно через
WebSocket, поэтому пускать туда мимо входа нельзя.

Зависимости: requests, itsdangerous (уже есть), cryptography (проверка RS256).
"""
from __future__ import annotations

import base64
import binascii
import html
import json
import re
import secrets
import time
from typing import Any, Optional
from urllib.parse import quote, urlencode

import requests
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.config import get_settings
from core.logs import get_logger

logger = get_logger(__name__)

COOKIE_NAME = "y360_session"
STATE_COOKIE = "y360_state"
SESSION_SALT = "y360-admin-session"

# Пути без сессии: вход, колбэк, выход и статика NiceGUI.
# ВАЖНО: /_nicegui_ws (WebSocket NiceGUI) сюда НЕ входит — он защищён сессией.
PUBLIC_PREFIXES = ("/login", "/oauth2/callback", "/logout", "/auth/me",
                   "/_nicegui/", "/favicon.ico", "/robots.txt")

_METADATA_TTL = 3600
_metadata: dict[str, Any] = {}
_metadata_at = 0.0
_jwks: dict[str, Any] = {}
_jwks_at = 0.0


# ---------------------------------------------------------------- утилиты

def _serializer() -> URLSafeTimedSerializer:
    cfg = get_settings()
    if not cfg.session_secret:
        raise RuntimeError("AUTH_MODE требует SESSION_SECRET — задайте длинную случайную строку в .env")
    return URLSafeTimedSerializer(cfg.session_secret, salt=SESSION_SALT)


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def public_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def session_from_cookie(raw: Optional[str]) -> Optional[dict]:
    """Проверяет подпись и срок куки сессии; возвращает данные или None."""
    if not raw:
        return None
    cfg = get_settings()
    try:
        data = _serializer().loads(raw, max_age=cfg.auth_session_hours * 3600)
    except (BadSignature, SignatureExpired):
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось разобрать куку сессии: %s", exc)
        return None
    return data if isinstance(data, dict) else None


def make_session_cookie(data: dict) -> str:
    return _serializer().dumps(data)


def _cookie_value(scope: dict, name: str) -> Optional[str]:
    """Достаёт куку из ASGI-scope (работает и для http, и для websocket)."""
    for key, value in scope.get("headers") or []:
        if key != b"cookie":
            continue
        for part in value.decode("latin-1", errors="replace").split(";"):
            if "=" not in part:
                continue
            cookie_name, _, cookie_value = part.strip().partition("=")
            if cookie_name == name:
                return cookie_value
    return None


def _dn_cn(dn: str) -> str:
    """Из DN вида CN=Группа,OU=... достаёт имя группы."""
    match = re.match(r"\s*[Cc][Nn]\s*=\s*([^,]+)", dn or "")
    return match.group(1).strip() if match else (dn or "").strip()


# ------------------------------------------------------------------- OIDC

def oidc_metadata() -> dict:
    """Discovery-документ IdP (кэшируется на час)."""
    global _metadata, _metadata_at
    cfg = get_settings()
    if _metadata and time.time() - _metadata_at < _METADATA_TTL:
        return _metadata
    url = f"{cfg.oidc_issuer}/.well-known/openid-configuration"
    resp = requests.get(url, timeout=cfg.request_timeout, verify=cfg.oidc_verify_tls)
    resp.raise_for_status()
    _metadata = resp.json()
    _metadata_at = time.time()
    logger.info("OIDC: получен discovery IdP %s", cfg.oidc_issuer)
    return _metadata


def oidc_jwks() -> dict:
    """Публичные ключи IdP для проверки подписи id_token (кэш на час)."""
    global _jwks, _jwks_at
    cfg = get_settings()
    if _jwks and time.time() - _jwks_at < _METADATA_TTL:
        return _jwks
    meta = oidc_metadata()
    resp = requests.get(meta["jwks_uri"], timeout=cfg.request_timeout, verify=cfg.oidc_verify_tls)
    resp.raise_for_status()
    _jwks = resp.json()
    _jwks_at = time.time()
    return _jwks


def _public_key(jwk: dict):
    from cryptography.hazmat.primitives.asymmetric import rsa

    if jwk.get("kty") != "RSA":
        raise ValueError(f"поддерживается только RSA, получено {jwk.get('kty')!r}")
    modulus = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    exponent = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    return rsa.RSAPublicNumbers(exponent, modulus).public_key()


def verify_id_token(token: str, nonce: str) -> dict:
    """Проверяет подпись RS256 и обязательные claims, возвращает claims.

    Проверяются подпись по ключам IdP, iss, aud, exp и nonce.
    Любое расхождение — исключение (fail closed).
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    cfg = get_settings()
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("id_token не похож на JWT")
    header = json.loads(_b64url_decode(parts[0]))
    claims = json.loads(_b64url_decode(parts[1]))
    signature = _b64url_decode(parts[2])

    if header.get("alg") != "RS256":
        raise ValueError(f"неподдерживаемый алгоритм подписи: {header.get('alg')!r}")

    keys = oidc_jwks().get("keys", [])
    if header.get("kid"):
        keys = [k for k in keys if k.get("kid") == header["kid"]] or keys
    signed = f"{parts[0]}.{parts[1]}".encode()
    verified = False
    for jwk in keys:
        try:
            _public_key(jwk).verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
            verified = True
            break
        except (InvalidSignature, ValueError, binascii.Error, KeyError):
            continue
    if not verified:
        raise ValueError("подпись id_token не подтверждена ключами IdP")

    now = time.time()
    if str(claims.get("iss", "")).rstrip("/") != cfg.oidc_issuer:
        raise ValueError(f"неверный iss: {claims.get('iss')!r}")
    aud = claims.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud]
    # ADFS в id_token кладёт в aud либо идентификатор клиента, либо идентификатор
    # Web API (ресурса) — принимаем оба, чтобы вход не зависел от версии ADFS.
    accepted = [cfg.oidc_client_id] + [
        part.strip() for part in (cfg.oidc_audience or "").split(",") if part.strip()
    ]
    if not any(item in aud_list for item in accepted):
        raise ValueError(f"неверный aud: {aud!r} (ожидался один из {accepted})")
    if claims.get("exp") and now > float(claims["exp"]) + 60:
        raise ValueError("id_token просрочен")
    if nonce and claims.get("nonce") != nonce:
        raise ValueError("nonce не совпал — возможна подмена ответа")
    return claims


def build_authorize_url() -> tuple[str, str, str]:
    """Возвращает (url для редиректа, state, nonce)."""
    cfg = get_settings()
    meta = oidc_metadata()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    params = {
        "client_id": cfg.oidc_client_id,
        "response_type": "code",
        "redirect_uri": cfg.oidc_redirect_uri,
        "scope": cfg.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "response_mode": "query",
    }
    if cfg.oidc_resource:
        # Явно просим токен для Web API ADFS: тогда ADFS применяет свою политику
        # доступа (разрешение конкретной группе), а не только нашу проверку.
        params["resource"] = cfg.oidc_resource
    return f"{meta['authorization_endpoint']}?{urlencode(params)}", state, nonce


def exchange_code(code: str) -> dict:
    cfg = get_settings()
    meta = oidc_metadata()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.oidc_redirect_uri,
        "client_id": cfg.oidc_client_id,
        "client_secret": cfg.oidc_client_secret,
    }
    resp = requests.post(meta["token_endpoint"], data=data,
                         timeout=cfg.request_timeout, verify=cfg.oidc_verify_tls)
    if resp.status_code != 200:
        raise RuntimeError(f"token endpoint вернул {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def claim_groups(claims: dict) -> list[str]:
    """Группы из токена: ADFS отдаёт их строкой, списком или в URI-ключе."""
    cfg = get_settings()
    wanted = cfg.oidc_group_claim.lower()
    raw = claims.get(cfg.oidc_group_claim)
    if raw is None:
        for key, value in claims.items():
            if key.rsplit("/", 1)[-1].lower() in (wanted, "groups", "role", "roles"):
                raw = value
                break
    if raw is None:
        return []
    if isinstance(raw, str):
        return [g.strip() for g in raw.split(",") if g.strip()]
    if isinstance(raw, list):
        return [str(g).strip() for g in raw if str(g).strip()]
    return [str(raw)]


def access_decision(claims: dict) -> tuple[bool, str]:
    """Пускать ли пользователя. Пустая OIDC_ADMIN_GROUP — любой аутентифицированный."""
    cfg = get_settings()
    if not cfg.oidc_admin_group:
        return True, ""
    wanted = cfg.oidc_admin_group.strip().lower()
    groups = claim_groups(claims)
    for group in groups:
        value = group.strip().lower()
        if value == wanted or value.rsplit("\\", 1)[-1] == wanted or wanted in value:
            return True, ""
    return False, (f"нет членства в группе «{cfg.oidc_admin_group}» "
                   f"(получены: {', '.join(sorted(groups)) or '—'})")


def user_from_claims(claims: dict) -> dict:
    login = (claims.get("upn") or claims.get("preferred_username") or claims.get("email")
             or claims.get("unique_name") or claims.get("sub") or "")
    return {
        "login": str(login),
        "name": str(claims.get("name") or claims.get("given_name") or login),
        "email": str(claims.get("email") or ""),
        "groups": claim_groups(claims),
    }


# ------------------------------------------------------------------- LDAP

def ldap_authenticate(login: str, password: str) -> tuple[bool, str, dict]:
    """Проверяет доменную учётку через LDAP-bind (AUTH_MODE=ldap).

    Сервисной учёткой находим пользователя по sAMAccountName, затем биндимся
    его логином и паролем — пароль нигде не сохраняется.
    """
    import ldap3

    from core.ad import ad_client_from_settings

    cfg = get_settings()
    if cfg.mock_ad:
        return False, "AD_SERVER=mock: вход по домену недоступен", {}
    login = (login or "").strip()
    if not login or not password:
        return False, "не указан логин или пароль", {}

    client = ad_client_from_settings()
    server_url = client._normalize_server(client.server, client.use_ssl)
    try:
        server = ldap3.Server(server_url, get_info=ldap3.NONE)
        with ldap3.Connection(server, user=client.bind_dn, password=client.bind_password,
                              auto_bind=True, receive_timeout=client.timeout) as conn:
            conn.search(
                client.base_dn,
                f"(&(objectClass=user)(sAMAccountName={ldap3.utils.conv.escape_filter_chars(login)}))",
                attributes=["distinguishedName", "displayName", "mail", "memberOf"])
            if not conn.entries:
                return False, "пользователь не найден в AD", {}
            entry = conn.entries[0]
            attrs = entry.entry_attributes_as_dict
            dn = str(entry.distinguishedName)
            groups = [_dn_cn(str(g)) for g in (attrs.get("memberOf") or [])]
            user = {
                "login": login,
                "name": str(entry.displayName or login),
                "email": str(entry.mail or ""),
                "groups": groups,
            }
        with ldap3.Connection(server, user=dn, password=password, auto_bind=True,
                              receive_timeout=client.timeout):
            pass  # bind прошёл — пароль верен
    except Exception as exc:  # noqa: BLE001
        logger.info("LDAP-вход %s не удался: %s", login, exc)
        return False, "неверный логин или пароль", {}

    if cfg.oidc_admin_group:
        wanted = cfg.oidc_admin_group.strip().lower()
        if not any(wanted == group.lower() for group in user["groups"]):
            return False, (f"нет членства в группе «{cfg.oidc_admin_group}» "
                           f"(получены: {', '.join(user['groups']) or '—'})"), user
    return True, "", user


# ---------------------------------------------------------------- маршруты

def install(app) -> None:
    """Подключает защиту и маршруты входа к приложению NiceGUI/FastAPI."""
    cfg = get_settings()
    if not cfg.auth_enabled:
        logger.info("Аутентификация выключена (AUTH_MODE=none): консоль открыта "
                    "всем, кому доступен порт")
        return
    if not cfg.session_secret:
        raise RuntimeError(f"AUTH_MODE={cfg.auth_mode} требует SESSION_SECRET в .env")
    if cfg.auth_mode == "oidc":
        missing = [name for name, value in (
            ("OIDC_ISSUER", cfg.oidc_issuer), ("OIDC_CLIENT_ID", cfg.oidc_client_id),
            ("OIDC_CLIENT_SECRET", cfg.oidc_client_secret),
            ("OIDC_REDIRECT_URI", cfg.oidc_redirect_uri)) if not value]
        if missing:
            raise RuntimeError("AUTH_MODE=oidc: не заполнено " + ", ".join(missing))

    app.add_middleware(AuthGuard)
    logger.info("Аутентификация включена: режим %s%s", cfg.auth_mode,
                f", группа {cfg.oidc_admin_group}" if cfg.oidc_admin_group else "")

    @app.get("/login")
    def login(request: Request, next: str = "/"):  # noqa: A002 — имя из URL
        if session_from_cookie(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/", status_code=302)
        if cfg.auth_mode == "ldap":
            return HTMLResponse(_login_form(next, error=""))
        url, state, nonce = build_authorize_url()
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(STATE_COOKIE,
                        _serializer().dumps({"state": state, "nonce": nonce, "next": next}),
                        max_age=600, httponly=True, samesite="lax",
                        secure=cfg.auth_cookie_secure)
        return resp

    @app.post("/login")
    async def login_post(request: Request):  # режим LDAP
        form = await request.form()
        ok, reason, user = ldap_authenticate(str(form.get("login", "")),
                                            str(form.get("password", "")))
        if not ok:
            logger.warning("Отказ во входе: %s (%s)", form.get("login"), reason)
            return HTMLResponse(_login_form(str(form.get("next", "/") or "/"), error=reason),
                                status_code=401)
        return _session_response(user, str(form.get("next", "/") or "/"))

    @app.get("/oauth2/callback")
    def callback(request: Request, code: str = "", state: str = "", error: str = "",
                 error_description: str = ""):
        if error:
            return HTMLResponse(_error_page(f"IdP вернул ошибку: {html.escape(error)} "
                                            f"{html.escape(error_description)}"), status_code=401)
        saved = session_from_cookie(request.cookies.get(STATE_COOKIE))
        if not saved or saved.get("state") != state:
            return HTMLResponse(_error_page("state не совпал — повторите вход"), status_code=400)
        try:
            tokens = exchange_code(code)
            claims = verify_id_token(tokens.get("id_token", ""), saved.get("nonce", ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ошибка входа OIDC: %s", exc)
            return HTMLResponse(_error_page(f"Не удалось подтвердить вход: {html.escape(str(exc))}"),
                                status_code=401)
        allowed, reason = access_decision(claims)
        if not allowed:
            logger.warning("Отказ во входе: %s", reason)
            return HTMLResponse(_error_page(f"Доступ запрещён: {html.escape(reason)}"), status_code=403)
        resp = _session_response(user_from_claims(claims), str(saved.get("next") or "/"))
        resp.delete_cookie(STATE_COOKIE)
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @app.get("/auth/me")
    def me(request: Request):
        data = session_from_cookie(request.cookies.get(COOKIE_NAME))
        return JSONResponse(data or {}, status_code=200 if data else 401)


def _session_response(user: dict, next_path: str) -> RedirectResponse:
    cfg = get_settings()
    target = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
    resp = RedirectResponse(target, status_code=302)
    resp.set_cookie(COOKIE_NAME, make_session_cookie(user),
                    max_age=cfg.auth_session_hours * 3600, httponly=True,
                    samesite="lax", secure=cfg.auth_cookie_secure)
    logger.info("Вход выполнен: %s", user.get("login"))
    return resp


# ------------------------------------------------------------------ защита

class AuthGuard:
    """ASGI-middleware: без валидной сессии страницы и WebSocket недоступны."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        cfg = get_settings()
        if not cfg.auth_enabled or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if public_path(scope.get("path", "/")):
            return await self.app(scope, receive, send)
        if session_from_cookie(_cookie_value(scope, COOKIE_NAME)):
            return await self.app(scope, receive, send)

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        target = scope.get("path", "/")
        redirect = RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=302)
        await redirect(scope, receive, send)


# ------------------------------------------------------------ HTML страниц

_STYLE = ("font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
          "background:#0b1120;color:#e2e8f0;display:flex;align-items:center;"
          "justify-content:center;height:100vh;margin:0")
_CARD = ("background:#111827;border:1px solid rgba(148,163,184,.15);border-radius:16px;"
         "padding:32px;max-width:520px;box-shadow:0 10px 30px rgba(2,6,23,.5)")
_BTN = ("background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border:0;"
        "border-radius:10px;padding:12px 20px;font-size:15px;cursor:pointer;width:100%")
_INPUT = ("width:100%;padding:11px;margin-bottom:10px;border-radius:10px;"
          "border:1px solid #334155;background:#0f172a;color:#e2e8f0;box-sizing:border-box")


def _login_form(next_path: str, error: str) -> str:
    error_html = (f'<p style="color:#fda4af;font-size:13px">{html.escape(error)}</p>'
                  if error else "")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Вход · Y360 Admin</title></head>
<body style="{_STYLE}"><div style="{_CARD}">
<h1 style="margin:0 0 6px;font-size:20px">Вход в консоль Y360 Admin</h1>
<p style="color:#94a3b8;font-size:13px;margin:0 0 18px">Доменная учётка Active Directory</p>
{error_html}
<form method="post" action="/login">
  <input type="hidden" name="next" value="{html.escape(next_path)}">
  <input name="login" placeholder="Логин" autofocus style="{_INPUT}">
  <input name="password" type="password" placeholder="Пароль" style="{_INPUT}">
  <button type="submit" style="{_BTN}">Войти</button>
</form></div></body></html>"""


def _error_page(message: str) -> str:
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Вход не выполнен · Y360 Admin</title></head>
<body style="{_STYLE}"><div style="{_CARD}">
<h1 style="margin:0 0 10px;font-size:20px">Вход не выполнен</h1>
<p style="color:#fda4af;font-size:14px">{message}</p>
<p style="margin-top:18px"><a href="/login" style="color:#a5b4fc">Попробовать снова</a></p>
</div></body></html>"""
