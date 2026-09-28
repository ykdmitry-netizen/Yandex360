#!/usr/bin/env python3
"""Диагностика перед правками (только чтение, ничего не меняет).

1. Проверяет «только в Яндексе» из таблицы dismissals по Active Directory:
   есть ли такие учётки в AD вообще (в т.ч. без СНИЛС) и включены ли они —
   чтобы не забэкапить и не запланировать удаление живого ящика.
2. Разбирает незавершённые каталоги снимков на NFS: размер mbox, признак
   обрыва (последняя запись), дубликаты по логинам, наличие manifest/архива.

Запуск: venv/bin/python scripts/diagnose_incomplete.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def check_ad_candidates() -> None:
    import ldap3

    from core.ad import ad_client_from_settings
    from core.config import get_settings
    from core.db import query_all

    cfg = get_settings()
    candidates = query_all("SELECT login, user_id FROM dismissals ORDER BY login")
    logins = [row["login"] for row in candidates]
    print(f"=== 1. Кандидаты на архивацию: {len(logins)} ===")

    client = ad_client_from_settings()
    server_url = client._normalize_server(client.server, client.use_ssl)
    server = ldap3.Server(server_url, get_info=ldap3.NONE)
    alive, disabled, missing = [], [], []
    with ldap3.Connection(server, user=client.bind_dn, password=client.bind_password,
                          auto_bind=True, receive_timeout=client.timeout) as conn:
        for login in logins:
            conn.search(
                client.base_dn,
                f"(&(objectClass=user)(sAMAccountName={ldap3.utils.conv.escape_filter_chars(login)}))",
                attributes=["sAMAccountName", "displayName", "userAccountControl",
                            cfg.ad_snils_attribute, "mail"])
            if not conn.entries:
                missing.append(login)
                continue
            attrs = conn.entries[0].entry_attributes_as_dict
            uac_values = attrs.get("userAccountControl") or [0]
            try:
                uac = int(uac_values[0])
            except (TypeError, ValueError):
                uac = 0
            enabled = not (uac & 0x2)          # ACCOUNTDISABLE
            snils = (attrs.get(cfg.ad_snils_attribute) or [""])[0]
            row = (login, "включена" if enabled else "отключена", bool(snils))
            (alive if enabled else disabled).append(row)

    print(f"   есть в AD и ВКЛЮЧЕНЫ (риск!): {len(alive)}")
    for login, _, has_snils in alive[:15]:
        print(f"      {login:<24} СНИЛС в AD: {'есть' if has_snils else 'нет'}")
    print(f"   есть в AD, но отключены: {len(disabled)}")
    for login, _, has_snils in disabled[:10]:
        print(f"      {login:<24} СНИЛС в AD: {'есть' if has_snils else 'нет'}")
    print(f"   вовсе отсутствуют в AD (норма для уволенных): {len(missing)}")
    print(f"      {', '.join(missing[:15])}")

    with ldap3.Connection(server, user=client.bind_dn, password=client.bind_password,
                          auto_bind=True, receive_timeout=client.timeout) as conn:
        conn.search(client.base_dn,
                    f"(&(objectClass=user)(!(cn=*$))(!({cfg.ad_snils_attribute}=*)))",
                    attributes=["sAMAccountName", "userAccountControl"], paged_size=500)
        total_nosnils = len(conn.entries)
        enabled_nosnils = 0
        for entry in conn.entries:
            uac_values = entry.entry_attributes_as_dict.get("userAccountControl") or [0]
            try:
                if not (int(uac_values[0]) & 0x2):
                    enabled_nosnils += 1
            except (TypeError, ValueError):
                pass
    print(f"=== Всего в AD без СНИЛС: {total_nosnils} (из них включённых: {enabled_nosnils}) ===")


def check_incomplete_snapshots() -> None:
    from core.config import get_settings

    cfg = get_settings()
    org_dir = Path(cfg.backup_root) / cfg.yandex_org_id
    print(f"\n=== 2. Незавершённые каталоги в {org_dir} ===")
    if not org_dir.exists():
        print("   каталог недоступен")
        return

    by_login: dict[str, list[dict]] = defaultdict(list)
    for login_dir in sorted(p for p in org_dir.iterdir() if p.is_dir()):
        for snap in sorted(p for p in login_dir.iterdir() if p.is_dir()):
            archive = snap.with_suffix(".tar.gz")
            manifest = snap / "manifest.json"
            mbox = snap / "mail.mbox"
            if archive.exists() and manifest.exists():
                continue
            size = mbox.stat().st_size if mbox.exists() else 0
            tail = b""
            if size:
                with mbox.open("rb") as fh:
                    fh.seek(max(0, size - 64))
                    tail = fh.read()
            by_login[login_dir.name].append({
                "stamp": snap.name, "size": size,
                "manifest": manifest.exists(),
                "complete_tail": tail.endswith(b"\n\n") or tail.endswith(b"\n"),
            })

    total = sum(len(v) for v in by_login.values())
    print(f"   логинов с незавершёнными: {len(by_login)}; каталогов: {total}; "
          f"суммарно {sum(r['size'] for v in by_login.values() for r in v) / 2**30:.1f} ГБ")
    for login, items in sorted(by_login.items(), key=lambda kv: -max(i["size"] for i in kv[1])):
        best = max(items, key=lambda i: i["size"])
        dupes = len(items) - 1
        print(f"   {login:<22} каталогов {len(items)} (дублей {dupes}); "
              f"лучший {best['stamp']}: {best['size'] / 2**20:.0f} МБ, "
              f"manifest={'да' if best['manifest'] else 'нет'}, "
              f"конец файла похож на завершённый: {'да' if best['complete_tail'] else 'НЕТ'}")


def main() -> int:
    check_ad_candidates()
    check_incomplete_snapshots()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
