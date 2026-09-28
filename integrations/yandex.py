# REST-клиент Яндекс 360 Directory / Mail API.
#
# Эндпоинты, не подтверждённые живой проверкой (mail-accounts, service
# applications), вынесены в отдельные методы — правятся в одном месте.
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

from core.logs import get_logger

logger = get_logger(__name__)

BASE_URL = "https://api360.yandex.net"
PER_PAGE = 50


class YandexAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class YandexAPIClient:
    """Клиент Directory API (пользователи, отделы) и вспомогательных сервисов."""

    def __init__(
        self,
        token: str = "",
        org_id: str | int = "",
        base_url: Optional[str] = None,
        parallel_requests: int = 10,
        timeout: int = 30,
        allow_local_emails: bool = False,
    ):
        self.token = token
        self.org_id = str(org_id)
        # YANDEX_API_BASE позволяет направить запросы на локальный мок-сервер.
        resolved_base = base_url or os.getenv("YANDEX_API_BASE") or BASE_URL
        self.base_url = resolved_base.rstrip("/")
        self.parallel_requests = max(1, int(parallel_requests))
        self.timeout = timeout
        self.allow_local_emails = allow_local_emails
        self.department_map: Dict[int, str] = {}
        self._last_departments_full: Dict[int, dict] = {}

        self.session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "PUT", "PATCH", "DELETE"}),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ------------------------------------------------------------------
    # HTTP-слой
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self) -> dict:
        return {
            "O-Token": self.token,
            "Authorization": f"OAuth {self.token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> Optional[requests.Response]:
        kwargs.setdefault("timeout", self.timeout)
        try:
            resp = self.session.request(method, self._url(path), headers=self._headers(), **kwargs)
        except requests.RequestException as exc:
            raise YandexAPIError(f"Сетевая ошибка {method} {path}: {exc}") from exc
        if resp.status_code == 404:
            logger.debug("404 для %s %s — ресурс пропущен", method, path)
            return None
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("message") or body.get("error_description") or str(body)[:300]
            except ValueError:
                detail = resp.text[:300]
            raise YandexAPIError(f"HTTP {resp.status_code} {method} {path}: {detail}", resp.status_code)
        return resp

    def _get_json(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        resp = self._request("GET", path, params=params)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise YandexAPIError(f"Невалидный JSON от {path}: {exc}") from exc

    @staticmethod
    def _extract_items(payload: Optional[dict], *keys: str) -> List[dict]:
        if not payload:
            return []
        embedded = payload.get("_embedded") or {}
        for key in keys:
            items = embedded.get(key) or payload.get(key)
            if isinstance(items, list):
                return items
        return []

    def _paginate(self, path: str, *keys: str, extra_params: Optional[dict] = None) -> List[dict]:
        items: List[dict] = []
        page = 1
        while True:
            params = {"perPage": PER_PAGE, "page": page}
            if extra_params:
                params.update(extra_params)
            payload = self._get_json(path, params=params)
            batch = self._extract_items(payload, *keys)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < PER_PAGE:
                break
            page += 1
        return items

    # ------------------------------------------------------------------
    # Подразделения
    # ------------------------------------------------------------------

    def _org_path(self) -> str:
        return f"/directory/v1/org/{self.org_id}"

    def list_departments(self) -> List[dict]:
        return self._paginate(f"{self._org_path()}/departments", "departments")

    def get_departments_full(self) -> Dict[int, dict]:
        result: Dict[int, dict] = {}
        for dept in self.list_departments():
            try:
                did = int(dept.get("id"))
            except (TypeError, ValueError):
                continue
            parent = dept.get("parentId")
            result[did] = {
                "name": dept.get("name", ""),
                "parentId": int(parent) if parent not in (None, "") else None,
            }
        self._last_departments_full = result
        return result

    def get_all_departments(self) -> Dict[int, str]:
        full = self.get_departments_full()
        self.department_map = {did: info["name"] for did, info in full.items()}
        return dict(self.department_map)

    def create_department(self, name: str, parent_id: Optional[int]) -> Optional[int]:
        body = {"name": name}
        if parent_id is not None:
            body["parentId"] = int(parent_id)
        resp = self._request("POST", f"{self._org_path()}/departments", json=body)
        if resp is None:
            return None
        try:
            new_id = resp.json().get("id")
            return int(new_id) if new_id is not None else None
        except (ValueError, TypeError):
            return None

    def delete_department(self, department_id: int) -> bool:
        return self._request("DELETE", f"{self._org_path()}/departments/{department_id}") is not None

    # ------------------------------------------------------------------
    # Пользователи
    # ------------------------------------------------------------------

    def list_users(self) -> List[dict]:
        return self._paginate(f"{self._org_path()}/users", "users", extra_params={"withGroups": "false"})

    def _user_path(self, user_id: str) -> str:
        return f"{self._org_path()}/users/{user_id}"

    def get_user_raw(self, user_id: str) -> Optional[dict]:
        return self._get_json(self._user_path(user_id), params={"_embed": "contacts"})

    def _build_yandex_user(self, raw: dict):
        from core.models import Contact, YandexUser

        emails: Dict[str, List[str]] = {"main": [], "alias": [], "other": []}
        phones: List[str] = []
        contacts: List[Contact] = []
        for c in raw.get("contacts") or []:
            if not isinstance(c, dict):
                continue
            contact = Contact.from_dict(c)
            if contact.type == "phone":
                phones.append(contact.value)
            elif contact.type == "email":
                if not contact.value:
                    continue
                if not self.allow_local_emails and contact.value.lower().endswith(".local"):
                    continue
                category = "main" if contact.main else "alias" if contact.alias else "other"
                emails[category].append(contact.value)
            contacts.append(contact)

        dept_id = raw.get("departmentId")
        if dept_id in (None, ""):
            dept_ids = raw.get("departmentIds") or []
            dept_id = dept_ids[0] if dept_ids else None

        main_email = (raw.get("email") or "").lower()
        if main_email.endswith(".local") and not self.allow_local_emails:
            main_email = ""

        user = YandexUser(
            id=str(raw.get("id", "")),
            nickname=raw.get("nickname", ""),
            display_name=raw.get("display_name") or raw.get("displayName") or "",
            department_id=str(dept_id) if dept_id not in (None, "") else None,
            department_name=self.department_map.get(int(dept_id), "Не указано") if dept_id not in (None, "") and str(dept_id).isdigit() else "Не указано",
            position=raw.get("position") or "",
            email=main_email or (emails["main"][0] if emails["main"] else ""),
            is_enabled=bool(raw.get("isEnabled", raw.get("status", "") == "active")),
            is_dismissed=bool(raw.get("isFired", raw.get("status") in ("fired", "deleted"))),
            is_admin=bool(raw.get("isAdministrator", raw.get("isAdmin", False))),
            employee_id=raw.get("employee_id") or raw.get("employeeId"),
        )
        user.contacts = contacts
        user.phones = phones
        user.emails = emails
        ext_ids = raw.get("externalIds") or []
        if isinstance(ext_ids, list):
            for item in ext_ids:
                if not isinstance(item, dict):
                    continue
                ext_type = str(item.get("type", "")).lower()
                ext_value = item.get("id") or item.get("value")
                if ext_type == "snils":
                    user.snils = ext_value
                    user.external_id = ext_value
                elif ext_type == "employeeid":
                    user.employee_id = ext_value
        return user

    def get_all_users(self, progress_callback: Optional[Callable[[str], None]] = None) -> list:
        def notify(msg: str):
            if progress_callback:
                progress_callback(msg)

        # Названия подразделений подставляются из карты: если её не загрузили
        # заранее, у всех сотрудников будет «Не указано». Подстраховываемся.
        if not self.department_map:
            self.get_all_departments()

        ids: List[str] = []
        page = 1
        while True:
            payload = self._get_json(
                f"{self._org_path()}/users",
                params={"perPage": PER_PAGE, "page": page, "fields": "id", "withGroups": "false"},
            )
            batch = self._extract_items(payload, "users")
            if not batch:
                break
            ids.extend(str(u.get("id")) for u in batch if u.get("id") is not None)
            notify(f"Список пользователей: загружено {len(ids)}")
            if len(batch) < PER_PAGE:
                break
            page += 1

        users = []
        total = len(ids)
        done = 0
        with ThreadPoolExecutor(max_workers=self.parallel_requests) as pool:
            futures = {pool.submit(self.get_user_raw, uid): uid for uid in ids}
            for future in as_completed(futures):
                uid = futures[future]
                done += 1
                try:
                    raw = future.result()
                except YandexAPIError as exc:
                    logger.warning("Не удалось получить пользователя %s: %s", uid, exc)
                    raw = None
                if raw:
                    try:
                        users.append(self._build_yandex_user(raw))
                    except Exception as exc:  # повреждённый профиль не роняет загрузку
                        logger.warning("Пропущен пользователь %s: %s", uid, exc)
                if done % 25 == 0 or done == total:
                    notify(f"Профили: {done}/{total}")
        return users

    def get_user_detail(self, user_id: str):
        raw = self.get_user_raw(user_id)
        if not raw:
            return None
        try:
            return self._build_yandex_user(raw)
        except Exception as exc:
            logger.error("Ошибка разбора профиля %s: %s", user_id, exc)
            return None

    def update_user(self, user_id: str, updates: dict) -> bool:
        try:
            return self._request("PATCH", self._user_path(user_id), json=updates) is not None
        except YandexAPIError as exc:
            logger.error("update_user(%s) не удался: %s", user_id, exc)
            return False

    def update_user_contacts(self, user_id: str, contacts: List[dict]) -> bool:
        try:
            return self._request("PUT", f"{self._user_path(user_id)}/contacts", json={"contacts": contacts}) is not None
        except YandexAPIError as exc:
            logger.error("update_user_contacts(%s) не удался: %s", user_id, exc)
            return False

    def create_user(self, payload: dict) -> dict:
        resp = self._request("POST", f"{self._org_path()}/users", json=payload)
        return resp.json() if resp is not None else {}

    # ------------------------------------------------------------------
    # Организация
    # ------------------------------------------------------------------

    def get_org(self) -> dict:
        """Информация об организации.

        Проверено на боевом API: пути /org/{id}/organization не существует
        (отдаёт null), а /directory/v1/org возвращает список организаций
        аккаунта — выбираем свою по org_id.
        """
        payload = self._get_json("/directory/v1/org")
        items = self._extract_items(payload, "organizations")
        if not items and isinstance(payload, dict) and payload.get("id"):
            return payload
        for org in items:
            if str(org.get("id")) == str(self.org_id):
                return org
        return items[0] if items else {}

    # ------------------------------------------------------------------
    # Почтовые ящики (mail-accounts) — пути проверить живым запросом
    # ------------------------------------------------------------------

    def get_user_mail_settings(self, user_id: str) -> dict:
        return self._get_json(f"/mail-accounts/v1/org/{self.org_id}/users/{user_id}/settings") or {}

    def list_shared_mailboxes(self) -> List[dict]:
        payload = self._get_json(f"/mail-accounts/v1/org/{self.org_id}/mailboxes/shared")
        return self._extract_items(payload, "mailboxes", "shared")

    def list_delegated_mailboxes(self) -> List[dict]:
        payload = self._get_json(f"/mail-accounts/v1/org/{self.org_id}/mailboxes/delegated")
        return self._extract_items(payload, "mailboxes", "delegated")

    # ------------------------------------------------------------------
    # Service applications (для token-exchange) — пути проверить живым запросом
    # ------------------------------------------------------------------

    def list_service_applications(self) -> List[dict]:
        payload = self._get_json(f"/security/v1/organizations/{self.org_id}/serviceApplications")
        items = self._extract_items(payload, "serviceApplications", "applications")
        result = []
        for app in items:
            result.append({
                "client_id": app.get("clientId") or app.get("client_id") or app.get("id"),
                "scopes": app.get("scopes") or [],
                "raw": app,
            })
        return result

    def activate_service_applications(self) -> bool:
        return self._request("PUT", f"/security/v1/organizations/{self.org_id}/services/activate", json={}) is not None

    def register_service_application(self, client_id: str, scopes: List[str]) -> bool:
        body = {"clientId": client_id, "scopes": scopes}
        return self._request("POST", f"/security/v1/organizations/{self.org_id}/serviceApplications", json=body) is not None

    def wait_available(self, seconds: float = 1.0) -> None:
        time.sleep(seconds)


def yandex_client_from_settings() -> YandexAPIClient:
    """Фабрика клиента по единой конфигурации (.env)."""
    from core.config import get_settings
    cfg = get_settings()
    return YandexAPIClient(
        token=cfg.yandex_token,
        org_id=cfg.yandex_org_id,
        base_url=cfg.yandex_api_base or None,
        parallel_requests=cfg.parallel_requests,
        timeout=cfg.request_timeout,
        allow_local_emails=cfg.allow_local_emails,
    )
