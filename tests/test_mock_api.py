"""HTTP-проверка мок-сервера Яндекс 360 (mocks/mock_yandex360.py, порт 8600).

Проверяются все ветки маршрутов: GET/POST/PATCH/PUT/DELETE.
Мутирующие проверки выполняются на временном пользователе, в конце состояние
мока возвращается к демо-данным (POST /mock/reset).

Запуск: <repo>\\venv\\Scripts\\python.exe tests\\test_mock_api.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8600"
ORG = "8365150"
D = f"{BASE}/directory/v1/org/{ORG}"
M = f"{BASE}/mail-accounts/v1/org/{ORG}"
S = f"{BASE}/security/v1/organizations/{ORG}"

RESULTS: list[tuple[bool, str, str]] = []


def call(method: str, url: str, body: dict | bytes | None = None,
         form: str | None = None, ctype: str = "application/json"):
    data = None
    if form is not None:
        data = form.encode()
        ctype = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", ctype)
    req.add_header("Authorization", "OAuth mock-token")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((bool(cond), name, detail))
    print(f"[{' OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # --- health / state ---
    st, body = call("GET", f"{BASE}/mock/health")
    check("GET /mock/health → 200 status=ok", st == 200 and body.get("status") == "ok",
          f"{st} users={body.get('users')} depts={body.get('departments')}")

    st, body = call("GET", f"{BASE}/mock/state")
    check("GET /mock/state → 200 с users/departments",
          st == 200 and "users" in body and "departments" in body,
          f"{st} users={len(body.get('users', []))}")

    # --- directory: users ---
    st, body = call("GET", f"{D}/users?page=1&perPage=5")
    users = (body or {}).get("_embedded", {}).get("users", [])
    check("GET users (пагинация) → 200, total и 5 записей",
          st == 200 and body.get("total", 0) > 0 and len(users) == 5,
          f"{st} total={body.get('total')} page={len(users)}")

    st, body = call("GET", f"{D}/users?fields=id")
    ids = (body or {}).get("_embedded", {}).get("users", [])
    check("GET users?fields=id → только идентификаторы",
          st == 200 and bool(ids) and all(set(u.keys()) == {"id"} for u in ids),
          f"{st} записей={len(ids)} ключи={sorted(ids[0].keys()) if ids else '-'}")

    if not users:
        print("Нет пользователей в моке — дальше проверять нечего")
        return 1
    uid = users[0]["id"]

    st, body = call("GET", f"{D}/users/{uid}")
    check(f"GET users/{uid} → карточка пользователя",
          st == 200 and body.get("id") == uid,
          f"{st} nickname={body.get('nickname')}")

    st, body = call("GET", f"{D}/users/99999999")
    check("GET users/<несуществующий> → 404", st == 404, f"{st} {body}")

    # --- directory: departments / organization / contacts ---
    st, body = call("GET", f"{D}/departments")
    check("GET departments → 200 со списком",
          st == 200 and body.get("total", 0) > 0,
          f"{st} total={body.get('total')}")

    st, body = call("GET", f"{D}/organization")
    check("GET organization → 200 с названием",
          st == 200 and bool(body.get("name")), f"{st} name={body.get('name')}")

    st, body = call("GET", f"{D}/users/{uid}/contacts")
    check("GET users/<uid>/contacts (нет маршрута → 404 мока)", st == 404, f"{st}")

    # --- mail accounts ---
    st, body = call("GET", f"{M}/users/{uid}/settings")
    check("GET mail settings → 200 с sender_info",
          st == 200 and "sender_info" in body, f"{st}")

    for kind in ("shared", "delegated"):
        st, body = call("GET", f"{M}/mailboxes/{kind}")
        n = len(body.get("_embedded", {}).get("mailboxes", []))
        check(f"GET mailboxes/{kind} → 200 (mailbox'ов: {n})", st == 200 and n > 0, f"{st}")

    st, body = call("GET", f"{S}/serviceApplications")
    check("GET serviceApplications → 200",
          st == 200 and "_embedded" in body, f"{st}")

    # --- token exchange (IMAP, RFC 8693) ---
    st, body = call("POST", f"{BASE}/token",
                    form="grant_type=urn:ietf:params:oauth:grant-type:token-exchange"
                         "&subject_token=ivanov&subject_token_type=urn:ietf:params:oauth:token-type:access_token")
    check("POST /token (token-exchange) → access_token",
          st == 200 and str(body.get("access_token", "")).startswith("mock-imap-"),
          f"{st} {body.get('access_token')}")

    st, body = call("POST", f"{BASE}/token", form="grant_type=password")
    check("POST /token (неверный grant) → 400", st == 400, f"{st} {body.get('error')}")

    # --- CRUD: создать пользователя, изменить, удалить ---
    st, body = call("POST", f"{D}/users",
                    {"nickname": "qa_test", "display_name": "QA Тестов", "position": "Тестировщик",
                     "email": "qa.test@demo360.test"})
    new_id = str((body or {}).get("id", ""))
    check("POST users → создан пользователь", st == 200 and new_id.isdigit(), f"{st} id={new_id}")

    if new_id:
        st, body = call("GET", f"{D}/users/{new_id}")
        check(f"GET созданного users/{new_id}", st == 200 and body.get("nickname") == "qa_test", f"{st}")

        st, body = call("PATCH", f"{D}/users/{new_id}",
                        {"position": "Старший тестировщик", "externalId": "12345678901",
                         "departmentId": 1})
        ok = st == 200 and body.get("position") == "Старший тестировщик"
        check("PATCH users → должность/СНИЛС/отдел обновлены",
              ok and body.get("externalIds") == [{"type": "snils", "id": "12345678901"}],
              f"{st} ext={body.get('externalIds')}")

        st, body = call("PUT", f"{D}/users/{new_id}/contacts",
                        {"contacts": [{"type": "email", "value": "qa.new@demo360.test", "main": True}]})
        st2, u = call("GET", f"{D}/users/{new_id}")
        check("PUT contacts → основной email обновлён",
              st == 200 and u.get("email") == "qa.new@demo360.test",
              f"{st} email={u.get('email')}")

        st, body = call("DELETE", f"{D}/users/{new_id}")
        st2, u = call("GET", f"{D}/users/{new_id}")
        check("DELETE users → status=deleted, isEnabled=false",
              st == 200 and u.get("status") == "deleted" and u.get("isEnabled") is False,
              f"{st} status={u.get('status')}")

        st, body = call("PATCH", f"{D}/users/99999999", {"position": "x"})
        check("PATCH несуществующего → 404", st == 404, f"{st}")

    # --- отделы: создание и удаление ---
    st, body = call("POST", f"{D}/departments", {"name": "QA-отдел", "parentId": 1})
    did = (body or {}).get("id")
    check("POST departments → создан отдел", st == 200 and did is not None, f"{st} id={did}")
    if did is not None:
        st, _ = call("DELETE", f"{D}/departments/{did}")
        check("DELETE departments → удалён", st == 200, f"{st}")
        st, _ = call("DELETE", f"{D}/departments/{did}")
        check("DELETE удалённого отдела → 404", st == 404, f"{st}")

    # --- service applications / activate ---
    st, body = call("POST", f"{S}/serviceApplications", {"clientId": "qa-client", "scopes": ["directory:read"]})
    check("POST serviceApplications → registered", st == 200 and body.get("status") == "registered", f"{st}")
    st, body = call("PUT", f"{S}/services/activate", {})
    check("PUT services/activate → activated", st == 200 and body.get("status") == "activated", f"{st}")

    # --- неизвестные пути ---
    st, _ = call("GET", f"{BASE}/unknown/path")
    check("GET неизвестного пути → 404", st == 404, f"{st}")
    st, _ = call("POST", f"{BASE}/unknown/path", {})
    check("POST неизвестного пути → 404", st == 404, f"{st}")

    # --- вернуть демо-состояние ---
    st, body = call("POST", f"{BASE}/mock/reset", {})
    check("POST /mock/reset → демо-данные восстановлены", st == 200, f"{st}")

    failed = [n for ok, n, _ in RESULTS if not ok]
    print("=" * 60)
    print(f"Проверок: {len(RESULTS)}; провалено: {len(failed)}")
    for n in failed:
        print(f"  FAIL: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
