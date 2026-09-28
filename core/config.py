# Единая конфигурация Y360 Admin: один .env — одна точка правды.
#
# Все модули проекта читают настройки только отсюда через get_settings().
# Локальный стенд по умолчанию работает на моках:
#   AD_SERVER=mock, IMAP_HOST=mock, YANDEX_API_BASE=http://127.0.0.1:8600
# Для продакшна достаточно заменить значения в .env — код не меняется.
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта (папка y360-admin)
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, str(default)).lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # --- Яндекс 360 ---
    yandex_token: str
    yandex_org_id: str
    # Пусто → боевой api360.yandex.net; в демо — локальный мок.
    yandex_api_base: str
    yandex_token_url: str  # token-exchange для IMAP (RFC 8693)
    yandex_client_id: str
    yandex_client_secret: str
    yandex_token_scope: str

    # --- Active Directory ---
    ad_server: str  # "mock" → встроенный LDAP-мок (core/ad_mock.py)
    ad_bind_dn: str
    ad_bind_password: str
    ad_base_dn: str
    ad_snils_attribute: str
    ad_use_ssl: bool

    # --- PostgreSQL ---
    pg_host: str
    pg_port: int
    pg_database: str
    pg_user: str
    pg_password: str

    # --- Почта и бэкапы ---
    imap_host: str  # "mock" → встроенный IMAP-мок (services/imap_mock.py)
    imap_port: int
    backup_root: Path
    restore_storage_mailbox: str
    retention_days: int
    delete_mailbox_after_days: int

    # --- Приложение ---
    ui_host: str
    ui_port: int
    ui_title: str
    parallel_requests: int
    request_timeout: int
    max_users: int  # 0 = без ограничения
    allow_local_emails: bool

    # --- Аутентификация консоли (см. web/auth.py) ---
    auth_mode: str            # none | oidc | ldap
    auth_session_hours: int   # срок жизни сессии
    auth_cookie_secure: bool  # ставить Secure у куки (нужен https)
    session_secret: str       # подпись куки сессии
    oidc_issuer: str          # https://sso.kolmar.ru/adfs
    oidc_client_id: str
    oidc_client_secret: str
    oidc_redirect_uri: str    # должен совпадать с зарегистрированным в ADFS
    oidc_scopes: str
    oidc_admin_group: str     # группа AD, которой разрешён вход ("" — любой доменный пользователь)
    oidc_group_claim: str     # имя claim с группами в id_token
    oidc_verify_tls: bool     # проверять сертификат IdP
    # логины, которые не берём в архивацию (через запятую)
    archive_exclude_logins: str

    @property
    def auth_enabled(self) -> bool:
        return self.auth_mode.strip().lower() in ("oidc", "ldap")

    @property
    def mock_ad(self) -> bool:
        return self.ad_server.strip().lower() in ("mock", "mock-ad", "mock-ad.local")

    @property
    def mock_imap(self) -> bool:
        return self.imap_host.strip().lower() == "mock"

    @property
    def mock_yandex(self) -> bool:
        return self.yandex_api_base.startswith("http://127.0.0.1") or self.yandex_api_base.startswith("http://localhost")

    @property
    def pg_params(self) -> dict:
        return {
            "host": self.pg_host,
            "port": self.pg_port,
            "dbname": self.pg_database,
            "user": self.pg_user,
            "password": self.pg_password,
        }

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            yandex_token=_env("YANDEX_TOKEN", "mock-token"),
            yandex_org_id=_env("YANDEX_ORG_ID", "8365150"),
            yandex_api_base=_env("YANDEX_API_BASE", "http://127.0.0.1:8600"),
            yandex_token_url=_env("YANDEX_TOKEN_URL", "http://127.0.0.1:8600/token"),
            yandex_client_id=_env("YANDEX_CLIENT_ID", "mock-client-id"),
            yandex_client_secret=_env("YANDEX_CLIENT_SECRET", "mock-client-secret"),
            yandex_token_scope=_env("YANDEX_TOKEN_SCOPE", "mail:imap_full"),
            ad_server=_env("AD_SERVER", "mock"),
            ad_bind_dn=_env("AD_BIND_DN", "CN=syncsvc,OU=Service,DC=home,DC=lab"),
            ad_bind_password=_env("AD_BIND_PASSWORD", "mock-bind-password"),
            ad_base_dn=_env("AD_BASE_DN", "DC=home,DC=lab"),
            ad_snils_attribute=_env("AD_SNILS_ATTRIBUTE", "snils"),
            ad_use_ssl=_env_bool("AD_USE_SSL", False),
            pg_host=_env("PGHOST", "127.0.0.1"),
            pg_port=_env_int("PGPORT", 5432),
            pg_database=_env("PGDATABASE", "y360_admin"),
            pg_user=_env("PGUSER", "y360_app"),
            pg_password=_env("PGPASSWORD", "y360_local_pw_2026"),
            imap_host=_env("IMAP_HOST", "mock"),
            imap_port=_env_int("IMAP_PORT", 993),
            backup_root=Path(_env("BACKUP_ROOT", str(ROOT / "data" / "backups"))),
            restore_storage_mailbox=_env("RESTORE_STORAGE_MAILBOX", "security-store@demo360.test"),
            retention_days=_env_int("RETENTION_DAYS", 365),
            delete_mailbox_after_days=_env_int("DELETE_MAILBOX_AFTER_DAYS", 90),
            ui_host=_env("UI_HOST", "127.0.0.1"),
            ui_port=_env_int("UI_PORT", 8080),
            ui_title=_env("UI_TITLE", "Y360 · Администрирование"),
            parallel_requests=_env_int("PARALLEL_REQUESTS", 4),
            request_timeout=_env_int("REQUEST_TIMEOUT", 20),
            max_users=_env_int("MAX_USERS", 0),
            allow_local_emails=_env_bool("ALLOW_LOCAL_EMAILS", False),
            auth_mode=_env("AUTH_MODE", "none").lower(),
            auth_session_hours=_env_int("AUTH_SESSION_HOURS", 12),
            auth_cookie_secure=_env_bool("AUTH_COOKIE_SECURE", False),
            session_secret=_env("SESSION_SECRET", ""),
            oidc_issuer=_env("OIDC_ISSUER", "").rstrip("/"),
            oidc_client_id=_env("OIDC_CLIENT_ID", ""),
            oidc_client_secret=_env("OIDC_CLIENT_SECRET", ""),
            oidc_redirect_uri=_env("OIDC_REDIRECT_URI", ""),
            oidc_scopes=_env("OIDC_SCOPES", "openid profile email"),
            oidc_admin_group=_env("OIDC_ADMIN_GROUP", ""),
            oidc_group_claim=_env("OIDC_GROUP_CLAIM", "group"),
            oidc_verify_tls=_env_bool("OIDC_VERIFY_TLS", True),
            archive_exclude_logins=_env("ARCHIVE_EXCLUDE_LOGINS", ""),
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Синглтон настроек (ленивая загрузка из .env)."""
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings
