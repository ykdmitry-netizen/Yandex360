"""Пайплайн синхронизации AD ↔ Яндекс 360 («Единый мастер»).

Порт scripts/master_cli.py проекта 08 без CLI/подпроцессов и порт
services/sync.py без Streamlit: все шаги выполняются в одном процессе и
доступны из UI и фоновых задач.

Шаги (ТЗ п.1–п.2):
  collect_data()          — выгрузка AD (только со СНИЛС) и Яндекс в снимок БД
  diff_report()           — отчёт о расхождениях по снимку
  build_department_plan() — какие отделы создать, чтобы никто не попал в корень
  apply_department_plan() — применение плана отделов (apply управляет записью)
  sync_users()            — синхронизация отдел/организация/должность AD → Яндекс
  publish_only_yandex()   — «только в Яндексе» → sync_events (+ закрытие вернувшихся)
  snapshot_status()       — что сейчас лежит в снимке и журнале аудита
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any, Callable, Optional

from core.compare import _normalize_text, compare_users
from core.db import load_snapshot, log_audit, query_all, save_snapshot
from core.logs import get_logger
from integrations.yandex import yandex_client_from_settings
from services.orgs import load_orgs_config

logger = get_logger(__name__)

TECHNICAL_OUS = ("domain controllers", "microsoft exchange", "computers",
                 "servers", "groups", "users", "administrators")

LogFunc = Optional[Callable[..., None]]


class PipelineError(RuntimeError):
    """Шаг мастера не выполнен (AD/Яндекс недоступны, снимок пуст, ошибка API)."""


def _make_logger(log_func: LogFunc) -> Callable[..., None]:
    def _log(msg: str, level: str = "info") -> None:
        logger.log({"error": 40, "warning": 30}.get(level, 20), msg)
        if log_func:
            log_func(msg, level)
    return _log


def load_snapshot_or_raise() -> tuple[list, list, dict]:
    data = load_snapshot()
    if not data:
        raise PipelineError("Снимок пуст — сначала выполните «Сбор данных» (collect)")
    return data


# ------------------------------------------------------------
# collect: выгрузка AD + Яндекс в снимок БД
# ------------------------------------------------------------

def collect_data(log_func: LogFunc = None) -> dict:
    from core.ad import ad_client_from_settings

    _log = _make_logger(log_func)
    client = yandex_client_from_settings()

    _log("Загрузка структуры подразделений Яндекс 360…")
    departments = client.get_all_departments()
    _log(f"Подразделений: {len(departments)}")

    _log("Загрузка пользователей Яндекс 360…")
    yandex_users = client.get_all_users(lambda msg: _log(msg))
    _log(f"Пользователей в Яндексе: {len(yandex_users)}")

    _log("Загрузка пользователей AD (только с заполненным СНИЛС)…")
    ad_users = ad_client_from_settings().collect_users()
    if not ad_users:
        raise PipelineError("AD не вернул пользователей — проверьте доступ к каталогу")
    _log(f"Пользователей в AD: {len(ad_users)}")

    saved = save_snapshot(yandex_users, ad_users, departments)
    _log("Снимок сохранён в БД" if saved else "Снимок НЕ сохранён (БД недоступна)",
         "info" if saved else "warning")

    comparison = compare_users(yandex_users, ad_users)
    stats = comparison.get("match_stats", {})
    _log(
        f"Сравнение: совпадают {len(comparison['matched'])}, расхождения "
        f"{len(comparison['mismatched'])} (логин {stats.get('login', 0)}, "
        f"СНИЛС {stats.get('snils', 0)}, employeeId {stats.get('employeeId', 0)}, "
        f"email {stats.get('email', 0)})"
    )
    published = publish_only_yandex(log_func=log_func)
    return {
        "yandex_users": len(yandex_users),
        "ad_users": len(ad_users),
        "departments": len(departments),
        "snapshot_saved": bool(saved),
        "counts": {
            "matched": len(comparison["matched"]),
            "mismatched": len(comparison["mismatched"]),
            "only_in_ad": len(comparison["only_in_ad"]),
            "only_in_yandex": len(comparison["only_in_yandex"]),
        },
        "published": published,
    }


# ------------------------------------------------------------
# diff: отчёт о расхождениях
# ------------------------------------------------------------

def diff_report() -> dict:
    yandex_users, ad_users, _depts = load_snapshot_or_raise()
    comparison = compare_users(yandex_users, ad_users)

    def diff_row(item: dict) -> dict:
        person = item.get("yandex_user") or item.get("ad_user")
        return {
            "login": item.get("login", ""),
            "name": person.display_name if person else "",
            "match_by": item.get("match_method", ""),
            "differences": [
                {"field": d["field"], "label": d["label"], "ad": d["ad"], "yandex": d["yandex"]}
                for d in item.get("differences", [])
            ],
        }

    return {
        "counts": {
            "yandex": len(yandex_users),
            "ad": len(ad_users),
            "matched": len(comparison["matched"]),
            "mismatched": len(comparison["mismatched"]),
            "only_in_ad": len(comparison["only_in_ad"]),
            "only_in_yandex": len(comparison["only_in_yandex"]),
        },
        "match_stats": comparison.get("match_stats", {}),
        "mismatched": [diff_row(i) for i in comparison["mismatched"][:500]],
        "matched": [{"login": i.get("login", ""), "match_by": i.get("match_method", "")}
                    for i in comparison["matched"][:500]],
        "only_in_ad": [{"login": u.sam_account_name, "name": u.display_name,
                        "department": u.department, "title": u.title}
                       for u in comparison["only_in_ad"][:500]],
        "only_in_yandex": [{"id": u.id, "login": u.nickname, "name": u.display_name,
                            "email": u.email, "department_name": u.department_name,
                            "position": u.position}
                           for u in comparison["only_in_yandex"][:500]],
    }


# ------------------------------------------------------------
# План создания отделов
# ------------------------------------------------------------

def build_department_plan(client=None, log_func: LogFunc = None) -> dict:
    """План: какие отделы создать, у кого отдел не найден (риск корня)."""
    from core.ad import ad_client_from_settings

    _log = _make_logger(log_func)
    yandex_users, ad_users, _depts = load_snapshot_or_raise()
    client = client or yandex_client_from_settings()

    _log("Читаю структуру подразделений Яндекс 360…")
    yandex_depts_full = client.get_departments_full()
    yandex_depts = {i: info["name"] for i, info in yandex_depts_full.items()}
    yandex_by_name: dict[str, list[dict]] = {}
    for dept_id, info in yandex_depts_full.items():
        yandex_by_name.setdefault(info["name"], []).append(
            {"id": dept_id, "parentId": info.get("parentId")}
        )

    _log("Читаю структуру OU из AD…")
    ous = ad_client_from_settings().get_organizational_units()
    ad_departments: dict[str, dict] = {}
    for ou in ous:
        path, name = ou["path"], ou["name"]
        if any(t in path.lower() for t in TECHNICAL_OUS):
            continue
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            sub_path = "/".join(parts[:i])
            if sub_path not in ad_departments:
                parent_path = "/".join(parts[:i - 1]) if i > 1 else None
                ad_departments[sub_path] = {
                    "name": parts[i - 1], "path": sub_path,
                    "parent_path": parent_path, "is_technical": False,
                }

    known_orgs, org_mapping = load_orgs_config()
    _log("Собираю отделы из карточек пользователей AD…")
    for user in ad_users or []:
        dept_name = user.department
        if not dept_name:
            continue
        org_name = None
        for short, full in org_mapping.items():
            if user.street_address and short.lower() in user.street_address.lower():
                org_name = full
                break
            if user.company and short.lower() in user.company.lower():
                org_name = full
                break
        if not org_name:
            org_name = next((k for k in known_orgs
                             if user.company and k.lower() in user.company.lower()), None)
        if not org_name:
            continue
        ad_departments.setdefault(org_name,
                                  {"name": org_name, "path": org_name,
                                   "parent_path": None, "is_technical": False})
        sub_path = f"{org_name}/{dept_name}"
        ad_departments.setdefault(sub_path,
                                  {"name": dept_name, "path": sub_path,
                                   "parent_path": org_name, "is_technical": False})

    org_ids: dict[str, int] = {}
    for org_name in set(known_orgs):
        normalized = _normalize_text(org_name)
        match = next((i for i, full in yandex_depts.items()
                      if _normalize_text(full) == normalized), None)
        if match is not None:
            org_ids[normalized] = match

    dept_users_count: Counter = Counter()
    for u in yandex_users or []:
        if u.department_id:
            dept_users_count[int(u.department_id)] += 1

    plan: dict[str, list] = {"to_create": [], "to_delete_duplicates": [],
                             "orphan_users": [], "to_reparent": []}
    existing_by_name: dict[str, list[dict]] = {}
    for dept_id, name in yandex_depts.items():
        existing_by_name.setdefault(_normalize_text(name), []).append(
            {"id": dept_id, "parentId": yandex_depts_full[dept_id].get("parentId")}
        )

    for path, info in ad_departments.items():
        if info["parent_path"] is None:
            continue
        parent_id = org_ids.get(_normalize_text(info["parent_path"]))
        if not parent_id:
            continue
        # Отдел достаточно создать один раз: задача ТЗ — чтобы сотрудник попал в
        # свой отдел, а не в корневой «Все пользователи». Поэтому совпадением
        # считается отдел с таким же именем в любом месте дерева.
        same_name = existing_by_name.get(_normalize_text(info["name"]), [])
        if not same_name:
            plan["to_create"].append({"name": info["name"], "parent_id": parent_id, "path": path})
        else:
            org_dept_ids = set(org_ids.values())
            misplaced = [i for i in same_name
                         if i["parentId"] in org_dept_ids and i["parentId"] != parent_id]
            if misplaced:
                # отдел уже есть, но висит в другой организации — перенос не
                # выполняем автоматически, только показываем в отчёте
                plan["to_reparent"].append({
                    "name": info["name"], "id": misplaced[0]["id"],
                    "current_parent_id": misplaced[0]["parentId"], "wanted_parent_id": parent_id,
                })

    for name, instances in yandex_by_name.items():
        if len(instances) <= 1:
            continue
        parents = {yandex_depts_full[i["parentId"]]["name"] for i in instances
                   if i.get("parentId") in yandex_depts_full}
        if len(parents) != 1:
            continue
        for inst in instances[1:]:
            if dept_users_count.get(inst["id"], 0) == 0:
                plan["to_delete_duplicates"].append(
                    {"id": inst["id"], "name": name, "parent_name": next(iter(parents))}
                )

    # Пользователи AD, чей отдел не найден в Яндексе, — риск попадания в корень.
    existing_names = {_normalize_text(n) for n in yandex_depts.values()}
    for user in ad_users or []:
        if user.department and _normalize_text(user.department) not in existing_names:
            plan["orphan_users"].append({"login": user.sam_account_name, "department": user.department})

    _log(f"Создать отделов: {len(plan['to_create'])}, "
         f"удалить пустых дублей: {len(plan['to_delete_duplicates'])}, "
         f"сотрудников без отдела в Яндексе: {len(plan['orphan_users'])}, "
         f"отделов не в той организации: {len(plan['to_reparent'])}")
    return plan


def apply_department_plan(plan: dict, apply: bool = False, log_func: LogFunc = None) -> dict:
    """Применяет план отделов. apply=False — только показывает (DRY-RUN)."""
    _log = _make_logger(log_func)
    client = yandex_client_from_settings()
    created = deleted = errors = 0

    for item in plan.get("to_create", []):
        if not apply:
            _log(f"DRY-RUN: создать «{item['name']}» в организации {item['parent_id']}")
            continue
        try:
            new_id = client.create_department(item["name"], item["parent_id"])
            if new_id:
                created += 1
                log_audit("create_dept", None,
                          after_data={"name": item["name"], "parent_id": item["parent_id"], "id": new_id})
                _log(f"Создан отдел «{item['name']}» (id {new_id})")
            else:
                errors += 1
                log_audit("create_dept", None, after_data={"name": item["name"]},
                          success=False, error="ошибка API")
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log_audit("create_dept", None, after_data={"name": item["name"]},
                      success=False, error=str(exc))
            _log(f"Ошибка создания «{item['name']}»: {exc}", "error")
        time.sleep(0.3)

    for item in plan.get("to_delete_duplicates", []):
        if not apply:
            _log(f"DRY-RUN: удалить дубль «{item['name']}» (id {item['id']})")
            continue
        try:
            if client.delete_department(item["id"]):
                deleted += 1
                log_audit("delete_dept", None, before_data={"name": item["name"], "id": item["id"]})
            else:
                errors += 1
                log_audit("delete_dept", None, before_data={"name": item["name"], "id": item["id"]},
                          success=False, error="ошибка API")
        except Exception as exc:  # noqa: BLE001
            errors += 1
            _log(f"Ошибка удаления дубля «{item['name']}»: {exc}", "error")
        time.sleep(0.3)

    return {"applied": bool(apply), "created": created, "deleted": deleted,
            "errors": errors, "planned_create": len(plan.get("to_create", [])),
            "planned_delete": len(plan.get("to_delete_duplicates", []))}


# ------------------------------------------------------------
# Синхронизация пользователей AD -> Яндекс
# ------------------------------------------------------------

def compute_updates(client, yandex_user, ad_user) -> tuple[dict, list[str], str | None]:
    """Поля для PATCH + отдел, который не удалось сопоставить (риск корня)."""
    updates: dict[str, Any] = {}
    fields: list[str] = []
    unresolved = None
    if ad_user.email_address and ad_user.email_address.lower() != (yandex_user.email or "").lower():
        updates["email"] = ad_user.email_address
        fields.append("email")
    if ad_user.department:
        norm = _normalize_text(ad_user.department)
        dept_id = next((i for i, name in client.department_map.items()
                        if _normalize_text(name) == norm), None)
        if dept_id and dept_id != (int(yandex_user.department_id) if yandex_user.department_id else None):
            updates["departmentId"] = dept_id
            fields.append("department")
        elif not dept_id:
            unresolved = ad_user.department
    if ad_user.title and ad_user.title != yandex_user.position:
        updates["position"] = ad_user.title
        fields.append("position")
    if ad_user.snils and ad_user.snils != (yandex_user.external_id or ""):
        updates["externalId"] = ad_user.snils
        fields.append("externalId")
    return updates, fields, unresolved


def merged_contacts(yandex_user, ad_user) -> list[dict] | None:
    """Контакты для PUT, если в AD есть email/телефон, которых нет в Яндексе.

    Контакты AD становятся основными, существующие контакты Яндекса сохраняются
    (email — нерезервированными).
    """
    current = {(c.type, c.value.lower()) for c in yandex_user.contacts}
    wanted = set()
    if ad_user.email_address:
        wanted.add(("email", ad_user.email_address.lower()))
    for phone in (ad_user.office_phone, ad_user.mobile_phone):
        if phone:
            wanted.add(("phone", phone.lower()))
    if not wanted - current:
        return None

    merged: list[dict] = []
    seen = set()
    if ad_user.email_address:
        merged.append({"type": "email", "value": ad_user.email_address,
                       "main": True, "alias": False, "synthetic": False})
        seen.add(("email", ad_user.email_address))
    for phone in (ad_user.office_phone, ad_user.mobile_phone):
        if phone:
            merged.append({"type": "phone", "value": phone,
                           "main": False, "alias": False, "synthetic": False})
            seen.add(("phone", phone))
    for contact in yandex_user.contacts:
        key = (contact.type, contact.value)
        if key in seen:
            continue
        data = contact.to_dict()
        if contact.type == "email":
            data["main"] = False
        merged.append(data)
        seen.add(key)
    return merged


def sync_users(apply: bool = False, log_func: LogFunc = None) -> dict:
    """Синхронизирует отдел/email/должность/СНИЛС и контакты AD → Яндекс.

    apply=False — предпросмотр (DRY-RUN): ничего не меняется, но отчёт полный.
    """
    _log = _make_logger(log_func)
    yandex_users, ad_users, _depts = load_snapshot_or_raise()
    client = yandex_client_from_settings()
    client.get_all_departments()  # наполняет department_map для сопоставления отделов
    comparison = compare_users(yandex_users, ad_users)

    updated = skipped = errors = 0
    details: list[dict] = []
    blocked: list[dict] = []
    for item in comparison["mismatched"]:
        yu, au = item.get("yandex_user"), item.get("ad_user")
        if not yu or not au:
            skipped += 1
            continue
        updates, fields, unresolved = compute_updates(client, yu, au)
        if unresolved:
            blocked.append({"login": item["login"], "department": unresolved})
            _log(f"{item['login']}: отдел «{unresolved}» нет в Яндексе — сначала создайте его",
                 "warning")
            continue
        contacts = merged_contacts(yu, au)
        if contacts:
            fields.append("contacts")
        if not updates and not contacts:
            skipped += 1
            continue
        if not apply:
            updated += 1
            details.append({"login": item["login"], "fields": fields, "dry_run": True})
            _log(f"DRY-RUN: {item['login']} — {', '.join(fields)}")
            continue
        before = yu.to_dict()
        ok = True
        if updates:
            ok = client.update_user(yu.id, updates)
            log_audit("update_user", item["login"], before_data=before, after_data=updates,
                      success=ok, error=None if ok else "ошибка API")
        if ok and contacts:
            ok = client.update_user_contacts(yu.id, contacts)
            log_audit("update_contacts", item["login"], before_data=before,
                      after_data={"contacts": contacts}, success=ok,
                      error=None if ok else "ошибка API")
        if ok:
            updated += 1
            details.append({"login": item["login"], "fields": fields, "dry_run": False})
            _log(f"Обновлён {item['login']} ({', '.join(fields)})")
        else:
            errors += 1
            _log(f"Ошибка обновления {item['login']}", "error")
        time.sleep(0.3)

    _log(f"Синхронизация: обновлено {updated}, без изменений {skipped}, "
         f"ошибок {errors}, без отдела в Яндексе {len(blocked)}")
    return {"applied": bool(apply), "updated": updated, "skipped": skipped,
            "errors": errors, "details": details[:500],
            "blocked_no_department": blocked[:500]}


# ------------------------------------------------------------
# «Только в Яндексе» -> события архивации
# ------------------------------------------------------------

def publish_only_yandex(log_func: LogFunc = None) -> dict:
    """Публикует «только в Яндексе» как кандидатов на увольнение и закрывает
    события тех, кто снова появился в AD. Возвращает статистику."""
    from services import integration

    _log = _make_logger(log_func)
    yandex_users, ad_users, _depts = load_snapshot_or_raise()
    comparison = compare_users(yandex_users, ad_users)
    only_yandex = comparison.get("only_in_yandex", []) or []
    # Сервисные (роботные) ящики и ручные исключения в архивацию не берём:
    # снимок по ним технически невозможен (IMAP для них закрыт).
    from services import filters
    candidates, excluded = filters.split_archivable(only_yandex)
    if excluded:
        _log("Исключены из архивации: " + ", ".join(f"{login} ({reason})" for login, reason in excluded))
    result = integration.publish_only_yandex(candidates)
    closed = integration.close_returned_events([u.sam_account_name for u in ad_users])
    _log(f"Кандидатов «только в Яндексе»: {len(only_yandex)}, "
         f"опубликовано {result.get('published', 0)}, закрыто вернувшихся в AD: {closed}")
    return {
        "candidates": len(candidates),
        "excluded": [{"login": login, "reason": reason} for login, reason in excluded],
        "published": result.get("published", 0),
        "skipped": result.get("skipped", 0),
        "closed": closed,
        "logins": [u.nickname for u in candidates][:200],
    }


# ------------------------------------------------------------
# Сводка по снимку и журналу
# ------------------------------------------------------------

def snapshot_status() -> dict:
    rows = query_all("SELECT source, count(*) AS count, max(snapshot_at) AS at "
                     "FROM users_snapshot GROUP BY source")
    out: dict[str, Any] = {"snapshot": {}, "audit": []}
    for row in rows:
        out["snapshot"][row["source"]] = {"count": row["count"], "at": str(row["at"] or "")}
    out["audit"] = query_all("SELECT operation, target_login, success, dry_run, error_message, created_at "
                             "FROM audit_log ORDER BY id DESC LIMIT 15")
    return out
