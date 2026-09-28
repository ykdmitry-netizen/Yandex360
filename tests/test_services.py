"""Проверки бизнес-логики Y360 Admin: сравнение, сроки хранения, хранилище
снимков, экспорт XLSX, восстановление писем, маппинги БД, события, мок-AD/IMAP.

Требует запущенных PostgreSQL и мок-API Яндекс 360 (scripts/start-stack.bat).
Тестовые данные пишутся в tests/_tmp и удаляются по завершении;
строки-артефакты в БД тоже подчищаются.

Запуск: <repo>\\venv\\Scripts\\python.exe tests\\test_services.py
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Корень репозитория: тесты лежат в <repo>/tests
REPO = Path(__file__).resolve().parents[1]
TMP = Path(__file__).resolve().parent / "_tmp"
sys.path.insert(0, str(REPO))

ORG = "8365150"
RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((bool(cond), name, detail))
    print(f"[{' OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def test_compare() -> None:
    section("core.compare: сопоставление AD ↔ Яндекс 360")
    from core.compare import _norm_snils, compare_users
    from core.models import ADUser, YandexUser

    check("Нормализация СНИЛС (форматы AD и Яндекса)",
          _norm_snils("123-456-789 00") == "12345678900" == _norm_snils("12345678900"),
          f"{_norm_snils('123-456-789 00')}")

    def y(**kw):
        base = dict(id="1", nickname="ivanov", display_name="Иванов Иван", department_name="ИТ",
                    email="ivanov@demo360.test", phones=[])
        base.update(kw)
        return YandexUser(**base)

    def a(**kw):
        base = dict(sam_account_name="ivanov", display_name="Иванов Иван", department="ИТ",
                    email_address="ivanov@demo360.test")
        base.update(kw)
        return ADUser(**base)

    res = compare_users([y()], [a()])
    check("Полное совпадение → matched, расхождений нет",
          len(res["matched"]) == 1 and not res["mismatched"],
          f"matched={len(res['matched'])} mismatched={len(res['mismatched'])}")

    res = compare_users([y()], [a(display_name="Иванов И.И.")])
    fields = [d["field"] for d in res["mismatched"][0]["differences"]] if res["mismatched"] else []
    check("Расхождение ФИО → mismatched с полем displayName",
          len(res["mismatched"]) == 1 and "displayName" in fields, f"поля: {fields}")

    res = compare_users([y(phones=["+7 000"])], [a(office_phone="+7 111")])
    fields = [d["field"] for d in res["mismatched"][0]["differences"]] if res["mismatched"] else []
    check("Расхождение телефонов → поле phone", "phone" in fields, f"поля: {fields}")

    res = compare_users(
        [y(nickname="new.login", snils=None, external_id="123-456-789 00")],
        [a(sam_account_name="old.login", snils="12345678900")])
    check("Fallback-матчинг по СНИЛС при смене логина",
          res["match_stats"]["snils"] == 1 and len(res["only_in_ad"]) == 0,
          f"stats={res['match_stats']}")

    res = compare_users(
        [y(nickname="emp.user", employee_id="EMP-77")],
        [a(sam_account_name="was.emp", employee_id="emp77")])
    check("Fallback-матчинг по employeeId",
          res["match_stats"]["employeeId"] == 1, f"stats={res['match_stats']}")

    res = compare_users(
        [y(nickname="by.mail", email="By.Mail@Demo360.test")],
        [a(sam_account_name="other.login", email_address="by.mail@demo360.test")])
    check("Fallback-матчинг по email",
          res["match_stats"]["email"] == 1, f"stats={res['match_stats']}")

    res = compare_users([y(), y(nickname="only.yandex", id="2")],
                        [a(), a(sam_account_name="only.ad", display_name="Только АД")])
    check("Пользователи только в AD / только в Яндексе",
          len(res["only_in_ad"]) == 1 and len(res["only_in_yandex"]) == 1,
          f"ad={len(res['only_in_ad'])} ya={len(res['only_in_yandex'])}")

    res = compare_users([y(), y(nickname="dup", id="9", external_id="12345678900")],
                        [a(snils="12345678900")])
    check("СНИЛС не сопоставляет одного пользователя Яндекса дважды",
          len(res["all_results"]) == 2, f"всего результатов: {len(res['all_results'])}")


def test_retention() -> None:
    section("services.retention: правила сроков хранения")
    from services import retention

    cases = [
        ("Генеральный директор", "Топ-менеджмент"),
        ("Заместитель генерального директора", "Топ-менеджмент"),
        ("Начальник отдела продаж", "Руководители"),
        ("Системный администратор", "ИТ и цифра"),
        ("Менеджер по закупкам", "Закупки и склад"),
        ("Кладовщик", "Закупки и склад"),
        ("Оператор call-центра", "Остальные"),
        (None, "Остальные"),
    ]
    for position, expected in cases:
        got = retention.preview(position)["rule_name"]
        check(f"Должность «{position}» → правило «{expected}»", got == expected, f"получено «{got}»")

    plan = retention.preview("Генеральный директор")
    months_ok = plan["label"] == "3 мес." and plan["deletion_at"] > datetime.now(timezone.utc)
    check("Топ-менеджмент — 3 месяца и дата удаления в будущем", months_ok, f"{plan['label']} {plan['deletion_at']}")

    days = (retention.preview("Оператор")["deletion_at"] - datetime.now(timezone.utc)).days
    check("Правило «Остальные» — 14 дней", 12 <= days <= 14, f"{days} дн.")

    rule_id = retention.save_rule(name="QA-тест правило", keywords="qa-тестировщик, qa",
                                  delete_after_amount=5, delete_after_unit="days", priority=500)
    try:
        check("save_rule → правило создано и применяется",
              retention.preview("QA-тестировщик")["rule_name"] == "QA-тест правило",
              retention.preview("qa-инженер")["rule_name"])
        retention.set_active(rule_id, False)
        check("set_active(False) → правило больше не выбирается",
              retention.preview("QA-тестировщик")["rule_name"] == "Остальные",
              retention.preview("QA-тестировщик")["rule_name"])
    finally:
        retention.delete_rule(rule_id)
    check("delete_rule → правило удалено",
          retention.preview("QA-тестировщик")["rule_name"] == "Остальные")


def test_storage() -> None:
    section("services.storage: снимки, манифест, sha256")
    from services.storage import Storage, sha256_of_file

    root = TMP / "storage"
    st = Storage(root)
    snap = st.prepare_snapshot(ORG, "qa_user")
    (snap / "mail.mbox").write_bytes(b"From qa@demo360.test\nSubject: QA\n\nbody\n")
    st.write_manifest(snap, org_id=ORG, login="qa_user", reason="manual", folders_count=1,
                      messages=1, size_bytes=snap.joinpath("mail.mbox").stat().st_size,
                      archive_name=snap.with_suffix(".tar.gz").name, archive_sha256="pending")
    archive, digest = st.archive_snapshot(snap)
    st.write_manifest(snap, org_id=ORG, login="qa_user", reason="manual", folders_count=1,
                      messages=1, size_bytes=snap.joinpath("mail.mbox").stat().st_size,
                      archive_name=archive.name, archive_sha256=digest)
    check("Снимок создан, архив .tar.gz посчитан",
          snap.is_dir() and archive.exists() and len(digest) == 64, f"sha256={digest[:16]}…")
    check("verify_snapshot → True для целого снимка", st.verify_snapshot(snap) is True)

    manifest = st.read_manifest(snap)
    check("manifest.json читается и содержит метаданные",
          bool(manifest) and manifest.get("login") == "qa_user" and manifest.get("messages") == 1,
          f"{sorted((manifest or {}).keys())}")

    check("sha256_of_file совпадает с хешем архива", sha256_of_file(archive) == digest)

    with archive.open("ab") as f:  # порча архива — контроль целостности должен сработать
        f.write(b"tampered")
    check("verify_snapshot → False после изменения архива (защита от подмены)",
          st.verify_snapshot(snap) is False)

    try:
        st.prepare_snapshot(ORG, "qa_user", )
        st2 = st.prepare_snapshot(ORG, "qa_user")
        raise AssertionError("ожидалась ошибка уникальности каталога")
    except FileExistsError as exc:
        check("Каталог снимка уникален (повторная запись не перезатирает прошлый)",
              True, type(exc).__name__)
    except AssertionError as exc:
        check("Каталог снимка уникален (повторная запись не перезатирает прошлый)", False, str(exc))

    old = st.mailbox_dir(ORG, "qa_user", stamp=(datetime.now() - timedelta(days=400)).strftime("%Y%m%d_%H%M%S"))
    old.mkdir(parents=True, exist_ok=True)
    removed = st.cleanup_older_than(keep_days=365)
    check("cleanup_older_than удаляет только устаревшие снимки",
          removed == 1 and not old.exists() and snap.exists(), f"удалено: {removed}")

    check("list_backups видит оставшийся снимок", len(st.list_backups(ORG, "qa_user")) == 1)


def test_export() -> None:
    section("services.export: отчёт XLSX")
    from io import BytesIO

    from openpyxl import load_workbook

    from core.compare import compare_users
    from core.models import ADUser, YandexUser
    from services import export

    ya = [YandexUser(id="1", nickname="ivanov", display_name="Иванов Иван", department_name="ИТ",
                     email="ivanov@demo360.test", position="Системный администратор", is_enabled=True),
          YandexUser(id="2", nickname="only.ya", display_name="Только Яндекс", department_name="Не указано")]
    ad = [ADUser(sam_account_name="ivanov", display_name="Иванов Иван", department="ИТ",
                 email_address="ivanov@demo360.test"),
          ADUser(sam_account_name="only.ad", display_name="Только АД", department="Бухгалтерия")]
    comparison = compare_users(ya, ad)

    payload = export.build_report(ya, ad, comparison)
    check("build_report возвращает XLSX-поток", isinstance(payload, bytes) and payload[:2] == b"PK",
          f"{len(payload)} байт")

    wb = load_workbook(BytesIO(payload))
    names = wb.sheetnames
    check("В книге есть листы Яндекс/AD/Расхождения",
          len(names) >= 3 and all(any(k in n for n in names) for k in ("Яндекс", "AD", "Расхожден")),
          f"листы: {names}")

    ws = wb[names[0]]
    header = [c.value for c in ws[1]]
    check("Шапка листа заполнена", bool(header) and any(header), f"{header[:6]}")
    check("Строк с данными больше одной", ws.max_row > 1, f"строк: {ws.max_row}")
    check("report_filename — xlsx с датой",
          export.report_filename().endswith(".xlsx") and len(export.report_filename()) > 10,
          export.report_filename())


def test_restore() -> None:
    section("services.restore: чтение снимка и экспорт писем")
    from services import restore
    from services.storage import get_storage

    storage = get_storage()
    backups = storage.list_backups(ORG, "belov")
    if not backups:
        check("Есть снимок ящика для проверки восстановления", False, "data/backups/8365150/belov пуст")
        return
    check("Снимок ящика belov найден", True, f"{len(backups)} снимк(ов)")

    snap = restore.find_snapshot(storage, ORG, "belov")
    check("find_snapshot возвращает каталог снимка", snap.is_dir(), snap.name)
    check("verify_snapshot (restore) подтверждает целостность", restore.verify_snapshot(storage, snap) is True)

    mbox = restore.ensure_mbox(snap)
    check("ensure_mbox находит/создаёт mail.mbox", mbox.exists(), f"{mbox.name} {mbox.stat().st_size} Б")

    entries = restore.read_index(snap, mbox)
    check("read_index: письма проиндексированы", len(entries) > 0, f"писем: {len(entries)}")
    folders = restore.folder_summary(entries)
    check("folder_summary: папки с числом писем",
          len(folders) > 0 and all({"folder", "messages"} <= set(f) for f in folders),
          f"{[(f['folder'], f['messages']) for f in folders]}")

    selected = restore.select_messages(entries, folders=[folders[0]["folder"]], limit=3)
    check("select_messages фильтрует по папке и limit",
          0 < len(selected) <= 3 and all(e["folder"] == folders[0]["folder"] for e in selected),
          f"выбрано: {len(selected)}")

    first = selected[0]
    raw = restore.read_message(mbox, first)
    check("read_message отдаёт RFC822-письмо без MBOX-разделителя",
          b"From " not in raw.split(b"\n")[0][:5] and b":" in raw[:200],
          f"{len(raw)} Б, начало: {raw[:40]!r}")

    out = TMP / "restore"
    eml = restore.export_eml([(restore.eml_name(first, 0), raw)], out / "eml")
    check("export_eml пишет .eml файл", len(eml) == 1 and eml[0].suffix == ".eml",
          f"{eml[0].name} ({eml[0].stat().st_size} Б)")

    mbox_out = restore.export_mbox([restore.read_record(mbox, first)], out / "one.mbox")
    check("export_mbox пишет .mbox с письмом", mbox_out.exists() and mbox_out.stat().st_size > 0,
          f"{mbox_out.stat().st_size} Б")

    count = restore.split_mbox_to_eml(mbox, out / "split")
    check("split_mbox_to_eml разбирает весь снимок на .eml", count == len(entries),
          f"файлов: {count} из {len(entries)} писем")

    extracted_mbox = restore.extract_to_dir(snap, out / "extract")
    check("extract_to_dir распаковывает архив снимка и отдаёт mail.mbox",
          extracted_mbox.is_file() and extracted_mbox.name == "mail.mbox"
          and extracted_mbox.stat().st_size > 0
          and (extracted_mbox.parent / "index.jsonl").exists(),
          f"{extracted_mbox.parent.name}/{extracted_mbox.name} ({extracted_mbox.stat().st_size} Б)")

    result = restore.restore_mailbox(ORG, "belov", out / "mailbox")
    check("restore_mailbox формирует план восстановления", isinstance(result, dict) and bool(result),
          f"ключи: {sorted(result)[:6]}")


def test_db_and_services() -> None:
    section("core.db и services: маппинги, запуски, события, увольнения")
    from core import db
    from services import dismissals, integration, state

    # --- маппинг отделов ---
    ad_dept = "QA-отдел-тест"
    db.set_department_mapping(ad_dept, 42, "QA-отдел", confirmed=True)
    rows = [r for r in db.get_department_mapping() if r["ad_department"] == ad_dept]
    check("Маппинг отдела AD → Яндекс сохраняется и читается",
          len(rows) == 1 and rows[0]["yandex_department_id"] == 42 and rows[0]["confirmed"] is True,
          f"{rows[0] if rows else '-'}")
    if rows:
        db.delete_department_mapping(rows[0]["id"])
    check("Маппинг отдела удаляется",
          not [r for r in db.get_department_mapping() if r["ad_department"] == ad_dept])

    # --- маппинг организаций ---
    db.set_organization_mapping("QA-КОРОТКО", 'ООО "QA Тест"')
    orgs = [r for r in db.get_organization_mapping() if r["short_name"] == "QA-КОРОТКО"]
    check("Маппинг организации сохраняется и читается",
          len(orgs) == 1 and orgs[0]["full_name"] == 'ООО "QA Тест"', f"{orgs[0] if orgs else '-'}")
    if orgs:
        db.execute("DELETE FROM organization_mapping WHERE id=%s", (orgs[0]["id"],))

    # --- настройки key/value ---
    original = state.get_setting("qa_test_key", "")
    state.set_setting("qa_test_key", "qa-value")
    check("Настройки key/value: запись и чтение",
          state.get_setting("qa_test_key") == "qa-value", state.get_setting("qa_test_key"))
    if original:
        state.set_setting("qa_test_key", original)
    else:
        db.execute("DELETE FROM settings WHERE key=%s", ("qa_test_key",))

    # --- запуски бэкапа ---
    run_id = state.start_run(ORG, "qa_login", reason="manual")
    state.log(run_id, "info", "QA: проверка журнала запуска")
    state.finish_run(run_id, 3, 1234, str(TMP / "qa"), sha256="a" * 64)
    runs = [r for r in state.recent_runs(50) if r["id"] == run_id]
    logs = [l for l in state.recent_logs(50) if l.get("run_id") == run_id] if runs else []
    check("Запуск бэкапа: старт → лог → завершение со sha256",
          bool(runs) and runs[0]["status"] == "success" and runs[0]["messages"] == 3
          and runs[0]["sha256"] == "a" * 64,
          f"{runs[0]['status'] if runs else '-'}, сообщений {runs[0]['messages'] if runs else '-'}")
    check("Логи запуска пишутся в backup_logs", len(logs) >= 1, f"записей: {len(logs)}")
    check("runs_for_user возвращает запуск пользователя",
          any(r["id"] == run_id for r in state.runs_for_user("qa_login", 50)))
    check("last_status_by_login отдаёт статус", state.last_status_by_login().get("qa_login") == "success",
          state.last_status_by_login().get("qa_login"))
    db.execute("DELETE FROM backup_logs WHERE run_id=%s", (run_id,))
    db.execute("DELETE FROM backup_runs WHERE id=%s", (run_id,))

    # --- аудит операций ---
    db.log_audit("qa_test_op", target_login="qa_audit", before_data={"a": 1}, after_data={"a": 2},
                 success=True, performed_by="qa", dry_run=True)
    db.log_audit("qa_test_op", target_login="qa_fail", success=False, error="QA: искусственная ошибка")
    audit = db.get_audit_log(limit=50, operation="qa_test_op")
    check("log_audit пишет операцию, get_audit_log её читает",
          len(audit) == 2 and {r["target_login"] for r in audit} == {"qa_audit", "qa_fail"},
          f"записей: {len(audit)}")
    failed_audit = db.get_audit_log(limit=50, operation="qa_test_op", only_failed=True)
    check("Фильтр «только ошибки» работает",
          len(failed_audit) == 1 and failed_audit[0]["target_login"] == "qa_fail",
          f"{[(r['target_login'], r['success']) for r in failed_audit]}")
    search_audit = db.get_audit_log(limit=50, search="qa_audit")
    check("Поиск по логину в аудите работает",
          len(search_audit) == 1 and search_audit[0]["dry_run"] is True, f"найдено: {len(search_audit)}")
    db.execute("DELETE FROM audit_log WHERE operation=%s", ("qa_test_op",))

    # --- события «только в Яндексе» ---
    integration.upsert_event("qa-evt-1", "qa_evt", email="qa_evt@demo360.test",
                             display_name="QA Событие", source="qa-test")
    integration.update_event_status("qa-evt-1", "backed_up", notes="QA: снимок готов", source="qa-test")
    evt = integration.get_event("qa-evt-1", source="qa-test")
    check("Событие sync_events: создание и смена статуса",
          bool(evt) and evt["status"] == "backed_up" and "QA" in (evt.get("notes") or ""),
          f"{evt['status'] if evt else '-'}")
    events = integration.list_events(status="backed_up", limit=200)
    check("list_events фильтрует по статусу",
          any(e["user_id"] == "qa-evt-1" for e in events), f"событий: {len(events)}")
    check("event_status_for_login возвращает статус события",
          integration.event_status_for_login("qa_evt") == "backed_up",
          str(integration.event_status_for_login("qa_evt")))
    db.execute("DELETE FROM sync_events WHERE source=%s", ("qa-test",))

    # --- увольнения ---
    # dismissals.user_id ссылается на users(id) — берём реального сотрудника без увольнения
    candidate = db.query_one(
        "SELECT u.id, u.login FROM users u "
        "WHERE u.id NOT IN (SELECT user_id FROM dismissals) ORDER BY u.id LIMIT 1")
    if not candidate:
        check("Есть сотрудник без записи об увольнении для проверки", False, "нет подходящего users.id")
    else:
        uid, login = candidate["id"], candidate["login"]
        created = dismissals.create_dismissal(uid, login, ORG, notes="QA: проверка")
        row = dismissals.get_dismissal(uid)
        check("create_dismissal создаёт запись увольнения", created == 1 and bool(row),
              f"вставлено: {created}, сотрудник {login}")
        check("Повторный create_dismissal не дублирует запись",
              dismissals.create_dismissal(uid, login, ORG) == 0)
        dismissals.update_notes(uid, "QA: заметка обновлена")
        row = dismissals.get_dismissal(uid)
        check("update_notes сохраняет примечание", (row or {}).get("notes") == "QA: заметка обновлена",
              str((row or {}).get("notes")))
        check("list_dismissals видит запись",
              any(d["user_id"] == uid for d in dismissals.list_dismissals(200)))
        check("dismissal_state: свежая запись без снимка → detected",
              dismissals.dismissal_state(row) == "detected",
              dismissals.dismissal_state(row))
        check("dismissal_state учитывает снимок и план удаления",
              dismissals.dismissal_state({"backup_done_at": 1, "deletion_scheduled_at": 1}) == "delete_scheduled"
              and dismissals.dismissal_state({"backup_done_at": 1}) == "backed_up"
              and dismissals.dismissal_state({"deletion_done_at": 1}) == "deleted")
        check("find_not_backed_up возвращает список кандидатов",
              any(d["user_id"] == uid for d in dismissals.find_not_backed_up()),
              f"{len(dismissals.find_not_backed_up())} записей без снимка")
        db.execute("DELETE FROM dismissals WHERE user_id=%s", (uid,))


def test_integrations() -> None:
    section("integrations и моки: AD, IMAP, Directory API")
    from core import ad_mock
    from core.ad import ad_client_from_settings
    from integrations.mail_tokens import exchange_token
    from integrations.yandex import yandex_client_from_settings
    from services import directory, imap_backup, imap_restore, orgs, pipeline

    check("AD-мок распознаётся по настройке", ad_mock.is_mock_requested("mock") is True)

    users = ad_mock.collect_ad_users()
    with_snils = [u for u in users if u.snils]
    check("AD-мок отдаёт сотрудников со СНИЛС",
          len(users) == 9 and len(with_snils) == len(users),
          f"пользователей: {len(users)}, со СНИЛС: {len(with_snils)}")
    check("AD-мок: карточки содержат логин, ФИО и отдел",
          all(u.sam_account_name and u.display_name and u.department for u in users),
          f"пример: {users[0].sam_account_name} / {users[0].department}")

    client = ad_client_from_settings()
    ad_users = client.collect_users()
    check("ADClient (боевой интерфейс) поверх мока отдаёт сотрудников",
          len(ad_users) == 9, f"{len(ad_users)} пользователей через {type(client).__name__}")

    yc = yandex_client_from_settings()
    ya_users = yc.get_all_users()
    check("Клиент Яндекс 360 (мок): get_all_users отдаёт карточки",
          isinstance(ya_users, list) and len(ya_users) >= 11, f"{len(ya_users)} записей")
    raw_users = yc.list_users()
    check("Клиент Яндекс 360 (мок): list_users отдаёт сырые записи",
          isinstance(raw_users, list) and len(raw_users) >= 11, f"{len(raw_users)} записей")
    depts = yc.list_departments()
    check("Клиент Яндекс 360 (мок): list_departments отдаёт отделы",
          isinstance(depts, list) and len(depts) > 0, f"{len(depts)} отделов")
    org = yc.get_org()
    check("Клиент Яндекс 360 (мок): get_org отдаёт организацию",
          isinstance(org, dict) and bool(org.get("name")), str(org.get("name"))[:40])

    listed = directory.list_users()
    check("directory.list_users читает справочник из БД", len(listed) == 11, f"{len(listed)} сотрудников")
    found = directory.find_user("ivanov")
    check("directory.find_user находит сотрудника", bool(found) and found.login == "ivanov",
          f"{found.name if found else '-'}")
    check("directory.list_departments читает отделы",
          len(directory.list_departments()) == 6, f"{len(directory.list_departments())}")
    check("directory.get_org читает организацию",
          bool(directory.get_org().get("name")), str(directory.get_org().get("name"))[:40])

    known, mapping = orgs.load_orgs_config()
    check("orgs.load_orgs_config читает data/orgs.json",
          len(known) == 10 and len(mapping) == 10, f"организаций: {len(known)}, маппингов: {len(mapping)}")

    status = pipeline.snapshot_status()
    check("pipeline.snapshot_status отдаёт снимок БД и аудит",
          isinstance(status, dict) and "snapshot" in status and "audit" in status,
          f"источники: {sorted(status['snapshot'])}")

    # --- IMAP: кодирование имён папок и подключение к моку ---
    for name in ("Отправленные", "Архив 2024", "INBOX"):
        encoded = imap_backup.encode_imap_folder_name(name)
        decoded = imap_backup._decode_imap_folder_name(encoded)
        check(f"IMAP-папка «{name}»: кодирование/декодирование", decoded == name,
              f"{encoded} → {decoded}")

    try:
        exchange_token("ivanov")
        check("token-exchange без email → понятная ошибка", False, "исключение не поднято")
    except Exception as exc:  # noqa: BLE001
        check("token-exchange без email → понятная ошибка",
              "email" in str(exc).lower(), f"{type(exc).__name__}: {exc}")

    token = exchange_token("ivanov", "ivanov@demo360.test")
    check("token-exchange IMAP выдаёт токен", str(token).startswith("mock-imap-"), str(token)[:24] + "…")

    conn = imap_backup.imap_connect("ivanov@demo360.test", token)
    folders = imap_backup.list_folders(conn)
    check("Мок-IMAP: подключение и список папок",
          len(folders) >= 3 and "INBOX" in folders, f"папки: {folders}")

    check("imap_restore.dest_folder формирует путь «Восстановлено/<логин>/<папка>»",
          imap_restore.dest_folder("ivanov", "INBOX") == "Восстановлено/ivanov/INBOX",
          imap_restore.dest_folder("ivanov", "INBOX"))
    targets = imap_restore.list_targets(ORG)
    check("imap_restore.list_targets: активные сотрудники + ящик-хранилище",
          len(targets) > 1 and any(t["kind"] == "storage" for t in targets),
          f"целей: {len(targets)}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    for func in (test_compare, test_retention, test_storage, test_export,
                 test_restore, test_db_and_services, test_integrations):
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            import traceback
            check(f"Блок {func.__name__} выполнен без исключений", False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc(limit=4)

    failed = [n for ok, n, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(f"Проверок: {len(RESULTS)}; провалено: {len(failed)}")
    for n in failed:
        print(f"  FAIL: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
