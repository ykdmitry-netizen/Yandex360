# Сравнение пользователей Яндекса и AD; утилиты нормализации.
#
# Матчинг: login → СНИЛС (11 цифр из externalId) → employeeId → email.
# API Яндекс 360 НЕ отдаёт поле snils — СНИЛС хранится в externalId
# (заполняется при синхронизации: updates['externalId'] = ad_user.snils).
from typing import Dict, List

from core.logs import get_logger
from core.models import YandexUser, ADUser

logger = get_logger(__name__)


def _normalize_text(s) -> str:
    """Нормализация текста для сравнения: регистр, пробелы, кавычки."""
    return (s or "").strip().lower().replace('"', '')


def _norm_id(v) -> str:
    """Нормализация идентификатора (СНИЛС/employeeId): регистр и разделители."""
    return (v or "").strip().lower().replace("-", "").replace(" ", "").replace("_", "")


def _norm_snils(v) -> str:
    """Извлекает из значения только цифры — для сопоставления СНИЛС.

    СНИЛС имеет формат из 11 цифр (XXX-XXX-XXX YY), но в AD и Яндексе он может
    храниться по-разному: '123-456-789 00', '12345678900', 'СНИЛС 123-456-789 00'
    и т.п. Если в значении ровно 11 цифр — возвращаем их (это надёжно опознаёт
    именно СНИЛС); иначе — обычную нормализацию (например, для employeeId).
    """
    digits = "".join(ch for ch in _norm_id(v) if ch.isdigit())
    return digits if len(digits) == 11 else _norm_id(v)


def _classify(results: Dict, yandex_user: YandexUser, ad_user: ADUser, login: str, match_method: str = "login"):
    """Добавляет пользователя в matched/mismatched в зависимости от расхождений."""
    differences = []
    if ad_user.display_name and ad_user.display_name != yandex_user.display_name:
        differences.append({"field": "displayName", "label": "Имя", "ad": ad_user.display_name, "yandex": yandex_user.display_name})
    if ad_user.department and _normalize_text(ad_user.department) != _normalize_text(yandex_user.department_name):
        differences.append({"field": "department", "label": "Отдел", "ad": ad_user.department, "yandex": yandex_user.department_name})
    if ad_user.email_address and ad_user.email_address.lower() != (yandex_user.email or "").lower():
        differences.append({"field": "email", "label": "Email", "ad": ad_user.email_address, "yandex": yandex_user.email})

    ad_phones = []
    if ad_user.office_phone and ad_user.office_phone.strip():
        ad_phones.append(ad_user.office_phone.strip())
    if ad_user.mobile_phone and ad_user.mobile_phone.strip():
        ad_phones.append(ad_user.mobile_phone.strip())
    ad_phones_sorted = sorted(set(ad_phones))
    yandex_phones_sorted = sorted(set(yandex_user.phones))
    if ad_phones_sorted != yandex_phones_sorted:
        differences.append({
            "field": "phone", "label": "Телефон",
            "ad": ", ".join(ad_phones_sorted) if ad_phones_sorted else "(не указан)",
            "yandex": ", ".join(yandex_phones_sorted) if yandex_phones_sorted else "(не указан)"
        })

    if differences:
        results["mismatched"].append({
            "login": login, "yandex_login": yandex_user.nickname, "match_method": match_method,
            "ad_user": ad_user, "yandex_user": yandex_user, "differences": differences,
        })
        results["all_results"].append({
            "login": login, "status": "mismatched", "match_method": match_method,
            "ad_user": ad_user, "yandex_user": yandex_user, "differences": differences,
        })
    else:
        results["matched"].append({
            "login": login, "yandex_login": yandex_user.nickname, "match_method": match_method,
            "ad_user": ad_user, "yandex_user": yandex_user,
        })
        results["all_results"].append({
            "login": login, "status": "matched", "match_method": match_method,
            "ad_user": ad_user, "yandex_user": yandex_user, "differences": [],
        })


def compare_users(yandex_users: List[YandexUser], ad_users: List[ADUser]) -> Dict:
    results = {
        "matched": [], "mismatched": [], "only_in_ad": [], "only_in_yandex": [], "all_results": [],
        # Статистика способов сопоставления (для диагностики)
        "match_stats": {"login": 0, "snils": 0, "employeeId": 0, "email": 0},
    }
    yandex_dict = {u.nickname.lower(): u for u in yandex_users if u.nickname}
    used = set()

    # Вторичные индексы для fallback-матчинга (логин мог измениться).
    by_snils = {}
    by_emp = {}
    by_email = {}
    for u in yandex_users:
        # Кандидаты на СНИЛС: snils + externalId, в двух нормализациях
        for raw in (u.snils, u.external_id):
            for key in (_norm_snils(raw), _norm_id(raw)):
                if key:
                    by_snils.setdefault(key, u)
        # Кандидаты на employeeId: employeeId + externalId
        for raw in (u.employee_id, u.external_id):
            key = _norm_id(raw)
            if key:
                by_emp.setdefault(key, u)
        # Индекс по email (страховка, если externalId ещё не заполнен)
        email_key = _normalize_text(u.email)
        if email_key:
            by_email.setdefault(email_key, u)

    for ad_user in ad_users:
        if not ad_user.sam_account_name:
            continue
        login = ad_user.sam_account_name.lower()
        yandex_user = yandex_dict.get(login)
        if yandex_user:
            used.add(login)
            _classify(results, yandex_user, ad_user, login, match_method="login")
            results["match_stats"]["login"] += 1
            continue

        # Fallback 1: СНИЛС (уникален и никогда не меняется) — основной ключ
        fb = None
        method = None
        if ad_user.snils:
            fb = by_snils.get(_norm_snils(ad_user.snils)) or by_snils.get(_norm_id(ad_user.snils))
            if fb is not None:
                method = "snils"
        # Fallback 2: employeeId
        if fb is None and ad_user.employee_id:
            fb = by_emp.get(_norm_id(ad_user.employee_id))
            if fb is not None:
                method = "employeeId"
        # Fallback 3: email
        if fb is None and ad_user.email_address:
            fb = by_email.get(_normalize_text(ad_user.email_address))
            if fb is not None:
                method = "email"

        if fb is not None:
            fb_key = fb.nickname.lower() if fb.nickname else ("id:" + str(fb.id))
            if fb_key in used:
                # Уже сопоставлен с другим пользователем AD — оставляем только в AD
                fb = None
                method = None
            else:
                used.add(fb_key)
                _classify(results, fb, ad_user, login, match_method=method or "unknown")
                results["match_stats"][method] += 1
                continue

        results["only_in_ad"].append(ad_user)
        results["all_results"].append({
            "login": login, "status": "only_in_ad", "match_method": None,
            "ad_user": ad_user, "yandex_user": None, "differences": [],
        })

    for login, yandex_user in yandex_dict.items():
        if login not in used:
            results["only_in_yandex"].append(yandex_user)
            results["all_results"].append({
                "login": login, "status": "only_in_yandex", "match_method": None,
                "ad_user": None, "yandex_user": yandex_user, "differences": [],
            })

    total_matched = len(results["matched"]) + len(results["mismatched"])
    logger.info(
        f"Сравнение: совпадений {total_matched} "
        f"(по логину {results['match_stats']['login']}, "
        f"по СНИЛС {results['match_stats']['snils']}, "
        f"по employeeId {results['match_stats']['employeeId']}, "
        f"по email {results['match_stats']['email']}), "
        f"только AD {len(results['only_in_ad'])}, "
        f"только Яндекс {len(results['only_in_yandex'])}"
    )
    return results
