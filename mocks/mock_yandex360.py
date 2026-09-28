#!/usr/bin/env python3
"""Локальный мок Яндекс 360 API для демонстрации консоли (без доступа к бою).

Реализует эндпоинты, которые использует integrations/yandex.py, поверх
набора демо-данных (пользователи, отделы, ящики). Изменения (PATCH/PUT/POST/
DELETE) сохраняются в JSON-файл рядом со скриптом, чтобы результат
синхронизации был виден между запусками.

Запуск:  venv\\Scripts\\python.exe mocks\\mock_yandex360.py [--port 8600]
Сброс:   venv\\Scripts\\python.exe mocks\\mock_yandex360.py --reset
Клиент направляется переменной окружения YANDEX_API_BASE=http://127.0.0.1:8600
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent / "state.json"
ORG_ID = "8365150"
DOMAIN = "demo360.test"
_LOCK = threading.Lock()


def _user(uid, nick, disp, dept, position, snils=None, email=None, phone=None,
          mobile=None, extra_alias=None, fired=False):
    email = email or f"{nick}@{DOMAIN}"
    contacts = [{"type": "email", "value": email, "main": True, "alias": False, "synthetic": False}]
    if extra_alias:
        contacts.append({"type": "email", "value": extra_alias, "main": False, "alias": True, "synthetic": False})
    if phone:
        contacts.append({"type": "phone", "value": phone, "main": False, "alias": False, "synthetic": False})
    if mobile:
        contacts.append({"type": "phone", "value": mobile, "main": False, "alias": False, "synthetic": False})
    external_ids = []
    if snils:
        external_ids.append({"type": "snils", "id": snils})
    return {
        "id": str(uid),
        "nickname": nick,
        "display_name": disp,
        "name": {"last": disp.split(" ")[0], "first": disp.split(" ")[1] if len(disp.split(" ")) > 1 else ""},
        "departmentId": str(dept),
        "email": email,
        "status": "fired" if fired else "active",
        "isEnabled": not fired,
        "isFired": fired,
        "position": position,
        "contacts": contacts,
        "externalIds": external_ids,
        "companyId": int(ORG_ID),
    }


def seed_state() -> dict:
    departments = [
        {"id": 101, "name": "Генеральный директор", "parentId": None},
        {"id": 102, "name": "ИТ отдел", "parentId": 101},
        {"id": 103, "name": "Бухгалтерия", "parentId": 101},
        {"id": 104, "name": "Отдел продаж", "parentId": 101},
        {"id": 201, "name": "Техподдержка", "parentId": 102},
        {"id": 999, "name": "Техподдержка", "parentId": 102},
    ]
    users = [
        _user(1001, "ivanov", "Иванов Иван Иванович", 101, "Генеральный директор",
              snils="11223344595", phone="+7 (495) 100-00-01"),
        _user(1002, "petrova", "Петрова Анна Сергеевна", 103, "Главный бухгалтер",
              snils="22334455696", phone="+7 (495) 100-00-02"),
        _user(1003, "sidorov", "Сидоров Павел Николаевич", 102, "Системный администратор",
              snils="33445566797", phone="+7 (495) 100-00-03",
              email=f"sidorov.old@{DOMAIN}"),
        _user(1004, "kosygin2024", "Косыгин Владимир Аркадьевич", 102, "Программист",
              snils="44556677898", phone="+7 (495) 100-00-04"),
        _user(1005, "vorobyova_a", "Воробьянова Мария Дмитриевна", 104, "Менеджер по продажам",
              snils="55667788990", phone="+7 (495) 100-00-05"),
        _user(1006, "smirnova", "Смирнова Елена Викторовна", 103, "Бухгалтер",
              snils="66778899091", phone="+7 (495) 100-00-06",
              extra_alias=f"smirnova.e@{DOMAIN}"),
        _user(1007, "belov", "Белов Дмитрий Олегович", 102, "Системный администратор",
              snils="77889900192", phone="+7 (495) 100-00-07", mobile="+7 (916) 200-00-07"),
        _user(1008, "zakharova", "Захарова Ольга Петровна", 104, "Руководитель отдела продаж",
              snils="88990011293", phone="+7 (495) 100-00-08"),
        _user(1009, "orlova", "Орлова Наталья Ивановна", 104, "Менеджер",
              snils="99001122394", phone="+7 (495) 100-00-09", fired=True),
        _user(1010, "trushkina", "Трушкина Вера Михайловна", 103, "Кассир",
              snils="00112233495", phone="+7 (495) 100-00-10", fired=True),
        _user(1011, "denisov", "Денисов Артём Юрьевич", 201, "Оператор поддержки",
              snils="11223344596", phone="+7 (495) 100-00-11"),
    ]
    return {
        "org": {"id": int(ORG_ID), "name": "ООО «Демо-Техника»", "locale": "ru", "status": "active"},
        "departments": departments,
        "users": users,
        "shared_mailboxes": [
            {"id": 9001, "email": f"office@{DOMAIN}", "nickname": "office", "name": "Офис"},
            {"id": 9002, "email": f"helpdesk@{DOMAIN}", "nickname": "helpdesk", "name": "HelpDesk"},
        ],
        "delegated_mailboxes": [
            {"id": 9101, "email": f"zakharova@{DOMAIN}", "delegateTo": f"ivanov@{DOMAIN}", "rights": "full"},
        ],
        "service_applications": [],
        "next_dept_id": 1000,
        "next_user_id": 2000,
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    state = seed_state()
    save_state(state)
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


STATES = load_state()

USER_RE = re.compile(rf"^/directory/v1/org/{ORG_ID}/users(?:/(?P<uid>[^/]+))?$")
DEPT_RE = re.compile(rf"^/directory/v1/org/{ORG_ID}/departments(?:/(?P<did>[^/]+))?$")
CONTACTS_RE = re.compile(rf"^/directory/v1/org/{ORG_ID}/users/(?P<uid>[^/]+)/contacts$")
ORG_RE = re.compile(rf"^/directory/v1/org/{ORG_ID}/organization$")
MAIL_SETTINGS_RE = re.compile(rf"^/mail-accounts/v1/org/{ORG_ID}/users/(?P<uid>[^/]+)/settings$")
MAILBOXES_RE = re.compile(rf"^/mail-accounts/v1/org/{ORG_ID}/mailboxes/(?P<kind>shared|delegated)$")
SVC_APP_RE = re.compile(rf"^/security/v1/organizations/{ORG_ID}/serviceApplications$")
SVC_ACT_RE = re.compile(rf"^/security/v1/organizations/{ORG_ID}/services/activate$")


def find_user(uid: str):
    return next((u for u in STATES["users"] if u["id"] == uid), None)


class Handler(BaseHTTPRequestHandler):
    server_version = "MockYandex360/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    # ---------------------------------------------------------- GET
    def do_GET(self):
        path = self.path.split("?")[0]
        query = self.path.split("?")[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

        if path == "/mock/health":
            return self._send(200, {"status": "ok", "users": len(STATES["users"]), "departments": len(STATES["departments"])})
        if path == "/mock/state":
            with _LOCK:
                return self._send(200, STATES)

        m = USER_RE.match(path)
        if m:
            with _LOCK:
                if m.group("uid"):
                    user = find_user(m.group("uid"))
                    return self._send(200, user) if user else self._send(404, {"message": "Ресурс не найден"})
                users = STATES["users"]
                if params.get("fields") == "id":
                    users = [{"id": u["id"]} for u in users]
                page = int(params.get("page", 1))
                per = int(params.get("perPage", 50))
                chunk = users[(page - 1) * per: page * per]
                return self._send(200, {"total": len(users), "_embedded": {"users": chunk}})

        m = DEPT_RE.match(path)
        if m:
            with _LOCK:
                page = int(params.get("page", 1))
                per = int(params.get("perPage", 50))
                chunk = STATES["departments"][(page - 1) * per: page * per]
                return self._send(200, {"total": len(STATES["departments"]), "_embedded": {"departments": chunk}})

        m = ORG_RE.match(path)
        if m:
            return self._send(200, STATES["org"])

        m = MAIL_SETTINGS_RE.match(path)
        if m:
            return self._send(200, {
                "sender_info": {"name": "Демо Пользователь", "replyTo": "", "signature": "-- Демо"},
                "address_book": {"contacts": [{"email": f"ivanov@{DOMAIN}", "name": "Иванов И.И."}]},
                "user_rules": [{"name": "Автоответ", "state": "disabled"}],
            })

        m = MAILBOXES_RE.match(path)
        if m:
            key = "shared_mailboxes" if m.group("kind") == "shared" else "delegated_mailboxes"
            return self._send(200, {"_embedded": {"mailboxes": STATES[key]}})

        m = SVC_APP_RE.match(path)
        if m:
            return self._send(200, {"_embedded": {"serviceApplications": STATES["service_applications"]}})

        return self._send(404, {"message": f"mock: неизвестный путь {path}"})

    # ---------------------------------------------------------- POST
    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}

        if path == "/token":
            from urllib.parse import parse_qs
            form = parse_qs(raw.decode("utf-8", errors="replace"))
            if form.get("grant_type", [""])[0].startswith("urn:ietf:params:oauth:grant-type:token-exchange"):
                subject = form.get("subject_token", ["unknown"])[0]
                import hashlib
                token = "mock-imap-" + hashlib.md5(subject.encode()).hexdigest()
                return self._send(200, {"access_token": token, "token_type": "bearer", "expires_in": 3600})
            return self._send(400, {"error": "unsupported_grant_type"})

        if path == "/mock/reset":
            global STATES
            with _LOCK:
                fresh = seed_state()
                STATES.clear()
                STATES.update(fresh)
                save_state(STATES)
            return self._send(200, {"status": "reset"})

        m = USER_RE.match(path)
        if m and not m.group("uid"):
            with _LOCK:
                uid = str(STATES["next_user_id"])
                STATES["next_user_id"] += 1
                new_user = _user(
                    uid,
                    body.get("nickname", f"user{uid}"),
                    body.get("display_name", "Новый Пользователь"),
                    body.get("departmentId", 0),
                    body.get("position", ""),
                    email=body.get("email"),
                )
                STATES["users"].append(new_user)
                save_state(STATES)
            return self._send(200, {"id": uid})

        m = DEPT_RE.match(path)
        if m and not m.group("did"):
            with _LOCK:
                did = STATES["next_dept_id"]
                STATES["next_dept_id"] += 1
                STATES["departments"].append({"id": did, "name": body.get("name", ""), "parentId": body.get("parentId")})
                save_state(STATES)
            return self._send(200, {"id": did})

        m = SVC_APP_RE.match(path)
        if m:
            with _LOCK:
                STATES["service_applications"].append({
                    "clientId": body.get("clientId"),
                    "scopes": body.get("scopes", []),
                })
                save_state(STATES)
            return self._send(200, {"status": "registered"})

        return self._send(404, {"message": f"mock: неизвестный POST {path}"})

    # ---------------------------------------------------------- PATCH
    def do_PATCH(self):
        path = self.path.split("?")[0]
        body = self._read_body()
        m = USER_RE.match(path)
        if m and m.group("uid"):
            with _LOCK:
                user = find_user(m.group("uid"))
                if not user:
                    return self._send(404, {"message": "Ресурс не найден"})
                for key, value in body.items():
                    if key == "externalId":
                        user["externalIds"] = [{"type": "snils", "id": value}] if value else []
                    elif key == "departmentId":
                        user["departmentId"] = str(value)
                    else:
                        user[key] = value
                save_state(STATES)
            return self._send(200, user)
        return self._send(404, {"message": f"mock: неизвестный PATCH {path}"})

    # ---------------------------------------------------------- PUT
    def do_PUT(self):
        path = self.path.split("?")[0]
        body = self._read_body()
        m = CONTACTS_RE.match(path)
        if m:
            with _LOCK:
                user = find_user(m.group("uid"))
                if not user:
                    return self._send(404, {"message": "Ресурс не найден"})
                user["contacts"] = body.get("contacts", [])
                main = next((c["value"] for c in user["contacts"] if c.get("type") == "email" and c.get("main")), user.get("email"))
                if main:
                    user["email"] = main
                save_state(STATES)
            return self._send(200, {"status": "ok"})
        if SVC_ACT_RE.match(path):
            return self._send(200, {"status": "activated"})
        return self._send(404, {"message": f"mock: неизвестный PUT {path}"})

    # ---------------------------------------------------------- DELETE
    def do_DELETE(self):
        path = self.path.split("?")[0]
        m = DEPT_RE.match(path)
        if m and m.group("did"):
            with _LOCK:
                did = int(m.group("did"))
                before = len(STATES["departments"])
                STATES["departments"] = [d for d in STATES["departments"] if d["id"] != did]
                save_state(STATES)
                if len(STATES["departments"]) == before:
                    return self._send(404, {"message": "нет такого отдела"})
            return self._send(200, {"status": "deleted"})
        m = USER_RE.match(path)
        if m and m.group("uid"):
            with _LOCK:
                uid = m.group("uid")
                user = find_user(uid)
                if not user:
                    return self._send(404, {"message": "Ресурс не найден"})
                user["status"] = "deleted"
                user["isEnabled"] = False
                save_state(STATES)
            return self._send(200, {"status": "deleted"})
        return self._send(404, {"message": f"mock: неизвестный DELETE {path}"})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8600)
    ap.add_argument("--reset", action="store_true", help="пересоздать демо-данные и выйти")
    args = ap.parse_args()

    if args.reset:
        save_state(seed_state())
        print(f"Демо-данные сброшены: {STATE_FILE}")
        return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Мок Яндекс 360 API: http://127.0.0.1:{args.port} (org {ORG_ID}, {len(STATES['users'])} пользователей)")
    server.serve_forever()


if __name__ == "__main__":
    main()
