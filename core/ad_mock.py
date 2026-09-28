# Встроенный мок Active Directory на ldap3.MockServer для локальной
# разработки и демонстрации без доменной машины. Включается значением
# "mock" в AD_SERVER (.env) или переменной окружения AD_MOCK=1.
import os
from typing import List, Optional

import ldap3

from core.models import ADUser

MOCK_HOST = "mock-ad.local"
MOCK_BASE_DN = "DC=home,DC=lab"

# Демо-пользователи согласованы с mocks/mock_yandex360.py:
# ivanov   — полное совпадение с Яндекс
# petrova  — в Яндексе устаревший телефон
# sidorov  — в Яндексе другой email и отдел
# kosygin  — есть только в AD
# vorobyov — есть только в AD (отключённая учётка)
# smirnova,belov,zakharova — совпадения
# gromova  — отдел «Хозяйственный отдел» отсутствует в Яндексе
#            (демо авто-создания отделов)
AD_DEMO_USERS: List[dict] = [
    {
        "sam": "ivanov", "cn": "Иванов Иван Иванович", "given": "Иван", "sn": "Иванов",
        "title": "Генеральный директор", "department": "Генеральный директор",
        "mail": "ivanov@demo360.test", "phone": "+7 (495) 100-00-01", "mobile": "",
        "snils": "11223344595", "employeeId": "EMP-001", "ou": "ИТ отдел", "enabled": True,
    },
    {
        "sam": "petrova", "cn": "Петрова Анна Сергеевна", "given": "Анна", "sn": "Петрова",
        "title": "Главный бухгалтер", "department": "Бухгалтерия",
        "mail": "petrova@demo360.test", "phone": "+7 (495) 100-00-02", "mobile": "+7 (916) 300-00-02",
        "snils": "22334455696", "employeeId": "EMP-002", "ou": "Бухгалтерия", "enabled": True,
    },
    {
        "sam": "sidorov", "cn": "Сидоров Павел Николаевич", "given": "Павел", "sn": "Сидоров",
        "title": "Системный администратор", "department": "Техподдержка",
        "mail": "sidorov@demo360.test", "phone": "+7 (495) 100-00-03", "mobile": "",
        "snils": "33445566797", "employeeId": "EMP-003", "ou": "ИТ отдел", "enabled": True,
    },
    {
        "sam": "kosygin", "cn": "Косыгин Владимир Аркадьевич", "given": "Владимир", "sn": "Косыгин",
        "title": "Программист", "department": "ИТ отдел",
        "mail": "kosygin@demo360.test", "phone": "+7 (495) 100-00-04", "mobile": "",
        "snils": "44556677898", "employeeId": "EMP-004", "ou": "ИТ отдел", "enabled": True,
    },
    {
        "sam": "vorobyov", "cn": "Воробьёв Олег Иванович", "given": "Олег", "sn": "Воробьёв",
        "title": "Смотритель", "department": "Отдел продаж",
        "mail": "vorobyov@demo360.test", "phone": "", "mobile": "",
        "snils": "55667788091", "employeeId": "EMP-005", "ou": "Отдел продаж", "enabled": False,
    },
    {
        "sam": "smirnova", "cn": "Смирнова Елена Викторовна", "given": "Елена", "sn": "Смирнова",
        "title": "Бухгалтер", "department": "Бухгалтерия",
        "mail": "smirnova@demo360.test", "phone": "+7 (495) 100-00-06", "mobile": "",
        "snils": "66778899091", "employeeId": "EMP-006", "ou": "Бухгалтерия", "enabled": True,
    },
    {
        "sam": "belov", "cn": "Белов Дмитрий Олегович", "given": "Дмитрий", "sn": "Белов",
        "title": "Системный администратор", "department": "ИТ отдел",
        "mail": "belov@demo360.test", "phone": "+7 (495) 100-00-07", "mobile": "+7 (916) 200-00-07",
        "snils": "77889900192", "employeeId": "EMP-007", "ou": "ИТ отдел", "enabled": True,
    },
    {
        "sam": "zakharova", "cn": "Захарова Ольга Петровна", "given": "Ольга", "sn": "Захарова",
        "title": "Руководитель отдела продаж", "department": "Отдел продаж",
        "mail": "zakharova@demo360.test", "phone": "+7 (495) 100-00-08", "mobile": "",
        "snils": "88990011293", "employeeId": "EMP-008", "ou": "Отдел продаж", "enabled": True,
    },
    # Громова — отдел «Хозяйственный отдел» отсутствует в Яндексе: демонстрирует
    # авто-создание недостающего отдела (иначе сотрудник попал бы в корень).
    {
        "sam": "gromova", "cn": "Громова Наталья Ивановна", "given": "Наталья", "sn": "Громова",
        "title": "Кладовщик", "department": "Хозяйственный отдел",
        "mail": "gromova@demo360.test", "phone": "+7 (495) 100-00-10", "mobile": "",
        "snils": "13579246803", "employeeId": "EMP-009", "ou": "Отдел продаж", "enabled": True,
    },
]

UAC_NORMAL = "512"
UAC_DISABLED = "546"


def is_mock_requested(server: str) -> bool:
    if os.getenv("AD_MOCK", "").strip().lower() in ("1", "true", "yes"):
        return True
    return (server or "").strip().lower() in ("mock", "mock-ad", "mock://", MOCK_HOST)


class _Value:
    def __init__(self, value):
        self._value = value

    def __bool__(self):
        return self._value is not None

    @property
    def value(self):
        return self._value


class _MockEntry:
    """Имитация ldap3.Entry: entry.<attr>.value, миссинг -> falsy _Value(None)."""

    def __init__(self, attrs: dict):
        self._attrs = attrs

    def __getattr__(self, name):
        return _Value(self._attrs.get(name))


def _filter_matches(search_filter: str, entry: dict) -> bool:
    import re
    conditions = re.findall(r"\(([\w!$-]+)=([^()]*)\)", search_filter)
    for attr, pattern in conditions:
        attr = attr.lower()
        if attr == "objectclass":
            value = entry.get("objectClass") or []
            values = value if isinstance(value, list) else [value]
        else:
            raw = entry.get(attr, entry.get(attr.lower()))
            values = [] if raw is None else ([raw] if isinstance(raw, str) else raw)
        if pattern == "*":
            if not values:
                return False
            continue
        import fnmatch
        if not any(fnmatch.fnmatch(str(v).lower(), pattern.lower()) for v in values):
            return False
    return True


class _MockPagedConnection:
    """Обёртка над MockConnection: реализует extend.standard.paged_search,
    отдавая демо-записи AD в формате результатов ldap3."""

    class _Extend:
        pass

    def __init__(self, mock_conn, entries: List[dict]):
        self._conn = mock_conn
        self._entries = entries
        self.extend = self._Extend()
        self.extend.standard = self._Extend()
        self.extend.standard.paged_search = self._paged_search
        self.entries = []

    def _matches(self, search_filter):
        return [e for e in self._entries if _filter_matches(search_filter, e)]

    def _paged_search(self, search_base, search_filter, search_scope, attributes=None,
                      paged_size=500, generator=False, **kw):
        results = []
        for entry in self._matches(search_filter):
            attrs = {k.lower(): ([v] if isinstance(v, str) else list(v)) for k, v in entry.items()}
            results.append({"type": "searchResEntry", "dn": entry["distinguishedName"], "attributes": attrs})
        return iter(results)

    def search(self, *a, **kw):
        search_filter = a[1] if len(a) > 1 else kw.get("search_filter", "")
        self.entries = [_MockEntry({k: v for k, v in e.items()}) for e in self._matches(search_filter)]
        return True

    def unbind(self):
        return True


def _build_entries() -> List[dict]:
    entries = [{
        "sAMAccountName": "syncsvc",
        "displayName": "Сервисная учётная запись",
        "objectClass": ["top", "person", "organizationalPerson", "user"],
        "distinguishedName": f"CN=syncsvc,OU=Service,{MOCK_BASE_DN}",
    }]
    for u in AD_DEMO_USERS:
        dn = f"CN={u['cn']},OU={u['ou']},{MOCK_BASE_DN}"
        entries.append({
            "objectClass": ["top", "person", "organizationalPerson", "user"],
            "sAMAccountName": u["sam"],
            "displayName": u["cn"],
            "givenName": u["given"],
            "sn": u["sn"],
            "title": u["title"],
            "department": u["department"],
            "company": "ООО Демо",
            "mail": u["mail"],
            "telephoneNumber": u["phone"],
            "mobile": u["mobile"],
            "snils": u["snils"],
            "employeeID": u["employeeId"],
            "userAccountControl": UAC_NORMAL if u["enabled"] else UAC_DISABLED,
            "l": "Москва",
            "co": "RU",
            "streetAddress": f"ул. Демо, д. {len(entries)}",
            "whenCreated": "20240115120000.0Z",
            "whenChanged": "20260901093000.0Z",
            "distinguishedName": dn,
        })
    for ou in ("Service", "ИТ отдел", "Бухгалтерия", "Отдел продаж"):
        entries.append({
            "objectClass": ["top", "organizationalUnit"],
            "name": ou,
            "distinguishedName": f"OU={ou},{MOCK_BASE_DN}",
        })
    return entries


_mock_entries = None


def get_mock_connection(bind_dn: str = "", bind_password: str = "", receive_timeout: int = 30):
    global _mock_entries
    if _mock_entries is None:
        _mock_entries = _build_entries()
    return _MockPagedConnection(None, _mock_entries)


def collect_ad_users(max_users: Optional[int] = None) -> List[ADUser]:
    conn = get_mock_connection()
    users = []
    for res in conn.extend.standard.paged_search(
        MOCK_BASE_DN, "(&(objectClass=user)(snils=*))", ldap3.SUBTREE, attributes=ldap3.ALL_ATTRIBUTES,
    ):
        from core.ad import ADClient
        data = ADClient._entry_to_dict.__get__(ADClient())(ADClient._as_paged_entry(res))
        if data.get("samAccountName"):
            users.append(ADUser.from_dict(data))
        if max_users and len(users) >= max_users:
            break
    return users
