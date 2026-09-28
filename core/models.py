# Модели данных: контакт, пользователь Яндекса, пользователь AD
# + строки таблиц БД (User, Department, BackupRun, Dismissal).
from datetime import datetime
from typing import List, Dict, Optional
from dataclasses import dataclass, field

from pydantic import BaseModel


@dataclass
class Contact:
    type: str
    value: str = ""
    main: bool = False
    alias: bool = False
    synthetic: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> 'Contact':
        if not isinstance(data, dict):
            data = {}
        return cls(
            type=data.get("type", ""),
            value=data.get("value", ""),
            main=bool(data.get("main", False)),
            alias=bool(data.get("alias", False)),
            synthetic=bool(data.get("synthetic", False)),
        )

    def to_dict(self) -> dict:
        return {"type": self.type, "value": self.value, "main": self.main, "alias": self.alias, "synthetic": self.synthetic}


@dataclass
class YandexUser:
    id: str
    nickname: str
    display_name: str = ""
    department_id: Optional[str] = None
    department_name: str = "Не указано"
    position: str = ""
    email: str = ""
    is_enabled: bool = False
    is_dismissed: bool = False
    is_admin: bool = False
    is_main_department: bool = False
    employee_id: Optional[str] = None
    snils: Optional[str] = None
    contacts: List[Contact] = field(default_factory=list)
    phones: List[str] = field(default_factory=list)
    emails: Dict[str, List[str]] = field(default_factory=dict)
    external_id: Optional[str] = None

    @property
    def main_email(self) -> Optional[str]:
        return self.emails.get("main", [""])[0] if self.emails.get("main") else None

    @property
    def all_emails(self) -> List[str]:
        result = []
        for category in ["main", "alias", "other"]:
            result.extend(self.emails.get(category, []))
        return result

    @classmethod
    def from_dict(cls, data: dict) -> 'YandexUser':
        contacts = []
        for c in data.get("contacts", []):
            if isinstance(c, dict):
                contacts.append(Contact.from_dict(c))
            elif isinstance(c, Contact):
                contacts.append(c)
        return cls(
            id=data.get("id", ""),
            nickname=data.get("nickname", ""),
            display_name=data.get("display_name", data.get("displayName", "")),
            department_id=data.get("department_id", data.get("departmentId")),
            department_name=data.get("department_name", data.get("departmentName", "Не указано")),
            position=data.get("position", ""),
            email=data.get("email", ""),
            is_enabled=data.get("is_enabled", data.get("isEnabled", False)),
            is_dismissed=data.get("is_dismissed", data.get("isDismissed", False)),
            is_admin=data.get("is_admin", data.get("isAdmin", False)),
            is_main_department=data.get("is_main_department", False),
            employee_id=data.get("employee_id", data.get("employeeId")),
            snils=data.get("snils"),
            contacts=contacts,
            phones=data.get("phones", []),
            emails=data.get("emails", {"main": [], "alias": [], "other": []}),
            external_id=data.get("external_id", data.get("externalId")),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nickname": self.nickname,
            "display_name": self.display_name,
            "department_id": self.department_id,
            "department_name": self.department_name,
            "position": self.position,
            "email": self.email,
            "is_enabled": self.is_enabled,
            "is_dismissed": self.is_dismissed,
            "is_admin": self.is_admin,
            "is_main_department": self.is_main_department,
            "employee_id": self.employee_id,
            "snils": self.snils,
            "contacts": [c.to_dict() for c in self.contacts],
            "phones": self.phones,
            "emails": self.emails,
            "external_id": self.external_id,
        }


@dataclass
class ADUser:
    sam_account_name: str
    display_name: str = ""
    given_name: str = ""
    surname: str = ""
    title: str = ""
    department: str = ""
    company: str = ""
    email_address: str = ""
    office_phone: str = ""
    mobile_phone: str = ""
    enabled: bool = False
    city: str = ""
    country: str = ""
    manager: Optional[str] = None
    employee_id: Optional[str] = None
    snils: Optional[str] = None
    created: Optional[str] = None
    modified: Optional[str] = None
    distinguished_name: str = ""
    street_address: str = ""

    @property
    def all_phones(self) -> List[str]:
        phones = []
        if self.office_phone:
            phones.append(self.office_phone)
        if self.mobile_phone:
            phones.append(self.mobile_phone)
        return phones

    @classmethod
    def from_dict(cls, data: dict) -> 'ADUser':
        return cls(
            sam_account_name=data.get("samAccountName", ""),
            display_name=data.get("displayName", ""),
            given_name=data.get("givenName", ""),
            surname=data.get("surname", ""),
            title=data.get("title", ""),
            department=data.get("department", ""),
            company=data.get("company", ""),
            email_address=data.get("emailAddress", ""),
            office_phone=data.get("officePhone", ""),
            mobile_phone=data.get("mobilePhone", ""),
            enabled=data.get("enabled", False),
            city=data.get("city", ""),
            country=data.get("country", ""),
            manager=data.get("manager"),
            employee_id=data.get("employeeId"),
            snils=data.get("snils"),
            created=data.get("created"),
            modified=data.get("modified"),
            distinguished_name=data.get("distinguishedName", ""),
            street_address=data.get("streetAddress", "")
        )

    def to_dict(self) -> dict:
        return {
            "samAccountName": self.sam_account_name,
            "displayName": self.display_name,
            "givenName": self.given_name,
            "surname": self.surname,
            "title": self.title,
            "department": self.department,
            "company": self.company,
            "emailAddress": self.email_address,
            "officePhone": self.office_phone,
            "mobilePhone": self.mobile_phone,
            "enabled": self.enabled,
            "city": self.city,
            "country": self.country,
            "manager": self.manager,
            "employeeId": self.employee_id,
            "snils": self.snils,
            "created": self.created,
            "modified": self.modified,
            "distinguishedName": self.distinguished_name,
            "streetAddress": self.street_address,
        }


# ------------------------------------------------------------
# Строки таблиц БД (совместимы с pydantic, лишние ключи игнорируются)
# ------------------------------------------------------------

class User(BaseModel):
    id: str
    org_id: str
    login: str
    nickname: Optional[str] = None
    name: Optional[str] = None
    position: Optional[str] = None
    department_id: Optional[str] = None
    email: Optional[str] = None
    snils: Optional[str] = None
    status: str = "active"


class Department(BaseModel):
    id: str
    name: str
    parent_id: Optional[str] = None


class BackupRun(BaseModel):
    id: int
    org_id: str
    login: str
    reason: str = "manual"
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: str = "running"
    messages: int = 0
    size_bytes: int = 0
    sha256: Optional[str] = None
    path: Optional[str] = None
    error: Optional[str] = None


class Dismissal(BaseModel):
    user_id: str
    login: str
    org_id: str
    fired_at: Optional[datetime] = None
    backup_done_at: Optional[datetime] = None
    backup_run_id: Optional[int] = None
    deletion_scheduled_at: Optional[datetime] = None
    deletion_done_at: Optional[datetime] = None
    retention_until: Optional[datetime] = None
    position: Optional[str] = None
    retention_rule: Optional[str] = None
    notes: Optional[str] = None
