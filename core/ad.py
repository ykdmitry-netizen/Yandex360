# Клиент Active Directory через LDAP/LDAPS (кросс-платформенно, без pyad/PowerShell).
#
# Ключевое бизнес-правило (этап 1 ТЗ): забираем пользователей ТОЛЬКО при
# заполненном атрибуте snils. При увольнении snils очищается — сотрудник
# пропадает из активной выгрузки, но ящик в Яндекс 360 остаётся.
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import ldap3

from core.logs import get_logger
from core.models import ADUser

logger = get_logger(__name__)


class ADClient:
    def __init__(self, server: str = "", bind_dn: str = "", bind_password: str = "", base_dn: str = "",
                 snils_attribute: str = "snils", max_users: Optional[int] = None, use_ssl: bool = True, timeout: int = 30):
        self.server = server
        self.bind_dn = bind_dn
        self.bind_password = bind_password
        self.base_dn = base_dn
        self.snils_attribute = snils_attribute
        self.max_users = max_users
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.last_error = None

    @staticmethod
    def _normalize_server(server: str, use_ssl: bool) -> str:
        s = (server or "").strip().rstrip("/")
        if not s:
            return ""
        if s.startswith("ldap://") or s.startswith("ldaps://"):
            return s
        scheme = "ldaps" if use_ssl else "ldap"
        return f"{scheme}://{s}"

    def _get_connection(self):
        from core.ad_mock import get_mock_connection, is_mock_requested
        if is_mock_requested(self.server):
            logger.info("AD_MOCK: используется встроенный мок Active Directory (без домена)")
            return get_mock_connection(self.bind_dn, self.bind_password, self.timeout)
        server_url = self._normalize_server(self.server, self.use_ssl)
        if not server_url:
            self.last_error = "Не указан адрес сервера AD"
            return None
        try:
            server = ldap3.Server(server_url, get_info=ldap3.ALL)
            conn = ldap3.Connection(server, user=self.bind_dn, password=self.bind_password,
                                    auto_bind=True, receive_timeout=self.timeout)
            return conn
        except Exception as e:
            self.last_error = f"Не удалось подключиться к AD по LDAP ({server_url}): {e}"
            logger.error(self.last_error)
            return None

    def test_connection(self) -> bool:
        conn = self._get_connection()
        if conn:
            conn.unbind()
            self.last_error = None
            return True
        return False

    def collect_users(self) -> List[ADUser]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            attributes = list(set([
                'sAMAccountName', 'displayName', 'givenName', 'sn',
                'title', 'department', 'company', 'mail',
                'telephoneNumber', 'mobile', 'homePhone', 'facsimileTelephoneNumber',
                'physicalDeliveryOfficeName', 'description', 'streetAddress',
                'l', 'postalCode', 'st', 'c', 'co', 'userAccountControl',
                'employeeID', 'manager', 'whenCreated', 'whenChanged',
                'distinguishedName', 'initials', self.snils_attribute,
            ]))
            search_filter = f"(&(objectClass=user)({self.snils_attribute}=*))"
            # Пагинированный поиск: обходит серверный лимит выдачи (обычно 1000)
            page_size = min(self.max_users, 500) if self.max_users else 500
            paged = conn.extend.standard.paged_search(
                self.base_dn,
                search_filter,
                ldap3.SUBTREE,
                attributes=attributes,
                paged_size=page_size,
            )
            users = []
            for res in paged:
                if self.max_users and len(users) >= self.max_users:
                    break
                entry = self._as_paged_entry(res)
                data = self._entry_to_dict(entry)
                if data.get("samAccountName"):
                    users.append(ADUser.from_dict(data))
            logger.info(f"Загружено пользователей из AD (LDAP): {len(users)}")
            self.last_error = None
            return users
        except Exception as e:
            self.last_error = f"Ошибка поиска пользователей в AD: {e}"
            logger.error(self.last_error)
            return []
        finally:
            try:
                conn.unbind()
            except Exception:
                pass

    @staticmethod
    def _as_paged_entry(res: Dict):
        """Адаптирует результат paged_search (dict) к виду, понятному _entry_to_dict."""
        attrs = res.get('attributes', {}) if isinstance(res, dict) else {}

        class _Val:
            def __init__(self, value):
                self.value = value

        class _Entry:
            @property
            def entry_attributes(self):
                return list(attrs.keys())

            def __getitem__(self, name):
                v = attrs.get(name.lower())
                if isinstance(v, list):
                    v = v[0] if len(v) == 1 else None
                return _Val(v)

        return _Entry()

    def get_organizational_units(self) -> List[Dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            conn.search(self.base_dn, "(objectClass=organizationalUnit)", ldap3.SUBTREE,
                        attributes=['name', 'distinguishedName'])
            ous = []
            for entry in conn.entries:
                name = entry.name.value if entry.name else ''
                dn = entry.distinguishedName.value if entry.distinguishedName else ''
                parts = dn.split(',')
                ou_parts = []
                for part in parts:
                    if part.strip().lower().startswith('ou='):
                        ou_parts.append(part.strip()[3:])
                if ou_parts:
                    ou_parts.reverse()
                    path = '/'.join(ou_parts)
                else:
                    path = name
                ous.append({
                    'name': name,
                    'path': path,
                    'distinguishedName': dn
                })
            logger.info(f"Загружено OU из AD: {len(ous)}")
            return ous
        except Exception as e:
            self.last_error = f"Ошибка получения OU: {e}"
            logger.error(self.last_error)
            return []
        finally:
            try:
                conn.unbind()
            except Exception:
                pass

    def _entry_to_dict(self, entry: Any) -> Dict:
        raw = {}
        for attr in entry.entry_attributes:
            try:
                raw[attr.lower()] = entry[attr].value
            except Exception:
                raw[attr.lower()] = None

        def g(name: str):
            return raw.get(name.lower())

        uac = g('userAccountControl')
        if isinstance(uac, str) and uac.isdigit():
            uac = int(uac)
        enabled = True
        if isinstance(uac, int):
            enabled = (uac & 0x0002) == 0
        country = g('co') or g('c')
        return {
            "samAccountName": g('sAMAccountName'),
            "displayName": g('displayName'),
            "givenName": g('givenName'),
            "surname": g('sn'),
            "initials": g('initials'),
            "title": g('title'),
            "department": g('department'),
            "company": g('company'),
            "emailAddress": g('mail'),
            "officePhone": g('telephoneNumber'),
            "mobilePhone": g('mobile'),
            "homePhone": g('homePhone'),
            "fax": g('facsimileTelephoneNumber'),
            "office": g('physicalDeliveryOfficeName'),
            "description": g('description'),
            "streetAddress": g('streetAddress'),
            "city": g('l'),
            "postalCode": g('postalCode'),
            "state": g('st'),
            "country": country,
            "enabled": enabled,
            "employeeId": g('employeeID'),
            "snils": g(self.snils_attribute),
            "manager": g('manager'),
            "created": self._fmt_date(g('whenCreated')),
            "modified": self._fmt_date(g('whenChanged')),
            "distinguishedName": g('distinguishedName'),
        }

    @staticmethod
    def _fmt_date(value) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.strftime('%Y-%m-%d %H:%M:%S')
        s = str(value)
        m = re.match(r'(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', s)
        if m:
            try:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                             int(m.group(4)), int(m.group(5)), int(m.group(6)))
                return d.strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                return None
        return s


def ad_client_from_settings():
    """Фабрика ADClient по единой конфигурации (.env)."""
    from core.config import get_settings
    cfg = get_settings()
    return ADClient(
        server=cfg.ad_server,
        bind_dn=cfg.ad_bind_dn,
        bind_password=cfg.ad_bind_password,
        base_dn=cfg.ad_base_dn,
        snils_attribute=cfg.ad_snils_attribute,
        max_users=cfg.max_users or None,
        use_ssl=cfg.ad_use_ssl,
        timeout=cfg.request_timeout,
    )
