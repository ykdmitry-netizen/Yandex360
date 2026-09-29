#!/usr/bin/env python3
"""Достаёт корневой сертификат внутреннего CA из Active Directory.

AD публикует сертификаты удостоверяющих центров в контейнере
CN=Certification Authorities,CN=Public Key Services,CN=Services,CN=Configuration,<base>
(атрибут cACertificate, бинарный DER). Скрипт сохраняет их в /tmp и печатает,
какие именно CA найдены — чтобы установить корень в доверенные на рабочих
машинах (иначе браузер не сохраняет куки входа и SSO не работает).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ldap3  # noqa: E402

from core.ad import ad_client_from_settings  # noqa: E402
from core.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    client = ad_client_from_settings()
    server = ldap3.Server(client._normalize_server(client.server, client.use_ssl), get_info=ldap3.NONE)
    base_dn = settings.ad_base_dn
    search_base = f"CN=Certification Authorities,CN=Public Key Services,CN=Services,CN=Configuration,{base_dn}"
    out_dir = Path("/tmp/ca-export")
    out_dir.mkdir(exist_ok=True)

    found = 0
    with ldap3.Connection(server, user=client.bind_dn, password=client.bind_password,
                          auto_bind=True, receive_timeout=60) as conn:
        conn.search(search_base, "(objectClass=certificationAuthority)",
                    attributes=["cn", "cACertificate"], paged_size=200)
        print(f"найдено записей CA: {len(conn.entries)}")
        for entry in conn.entries:
            name = str(entry.cn)
            certs = entry.entry_attributes_as_dict.get("cACertificate") or []
            for index, raw in enumerate(certs):
                data = raw if isinstance(raw, bytes) else bytes(raw)
                suffix = "" if len(certs) == 1 else f"-{index + 1}"
                path = out_dir / f"{name.replace(' ', '_')}{suffix}.cer"
                path.write_bytes(data)
                found += 1
                print(f"   {path}  ({len(data)} байт)  CN={name}")

        # Заодно проверим, что этот корень указан как доверенный для проверки
        # подлинности сертификатов пользователей (NTAuth).
        conn.search(f"CN=NTAuthCertificates,CN=Public Key Services,CN=Services,CN=Configuration,{base_dn}",
                    "(objectClass=*)", attributes=["cACertificate"])
        ntauth = conn.entries[0].entry_attributes_as_dict.get("cACertificate") if conn.entries else []
        print(f"сертификатов в NTAuthCertificates: {len(ntauth or [])}")

    print(f"итого сохранено файлов: {found}")
    return 0 if found else 1


if __name__ == "__main__":
    raise SystemExit(main())
