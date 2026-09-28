"""Мок IMAP-соединения для локальной разработки (IMAP_HOST=mock).

Реализует подмножество imaplib.IMAP4, которое используют services/imap_backup
и services/imap_restore: list / select / uid(search|fetch) / append / create /
noop / logout. Письма генерируются детерминированно от адреса ящика.
"""
from __future__ import annotations

import base64
import hashlib
from email.message import EmailMessage
from email.utils import formatdate


def _mod_utf7(name: str) -> str:
    if name.isascii():
        return name
    b64 = base64.b64encode(name.encode("utf-16-be")).decode("ascii").rstrip("=").replace("/", ",")
    return f"&{b64}-"


def _folder_key(name: str) -> str:
    """Ключ папки в mock-хранилище (не-ASCII имена — в modified UTF-7)."""
    return name if name.isascii() else _mod_utf7(name)


FOLDERS = ("INBOX", "Отправленные", "Архив 2024")

# Мок общий для процесса: снимки и APPEND видят одно и то же «состояние ящика»,
# иначе восстановленное письмо нельзя будет прочитать обратно новым соединением.
_SHARED_MAILBOXES: dict[str, dict[str, list[bytes]]] = {}


class MockIMAP4:
    def __init__(self, mailbox_email: str):
        self.email = mailbox_email
        seed = int(hashlib.md5(mailbox_email.encode()).hexdigest(), 16)
        self._messages = _SHARED_MAILBOXES.setdefault(
            mailbox_email,
            {
                _folder_key(f): [bytes(m) for m in self._build(f, seed, i)]
                for i, f in enumerate(FOLDERS)
            },
        )
        self._selected = None

    def _build(self, folder: str, seed: int, idx: int):
        count = 6 + (seed >> (idx * 4)) % 9
        msgs = []
        for n in range(1, count + 1):
            m = EmailMessage()
            m["Message-ID"] = f"<mock-{seed % 10**8}-{idx}-{n}@demo360.test>"
            m["Date"] = formatdate(1760000000 + n * 86400 + idx, localtime=False)
            if folder == "Отправленные":
                m["From"], m["To"] = self.email, f"partner{n % 3}@demo360.test"
            else:
                m["From"], m["To"] = f"partner{n % 3}@demo360.test", self.email
            m["Subject"] = f"[{folder}] Демонстрационное письмо №{n}"
            m.set_content(
                f"Здравствуйте!\n\nЭто письмо №{n} из папки «{folder}» ящика {self.email}.\n"
                f"Оно сгенерировано моком IMAP для проверки снимков MBOX.\n\n-- Демо-система\n"
            )
            msgs.append(m)
        return msgs

    # ---- API imaplib ----

    def list(self, *a, **kw):
        return "OK", [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self._messages]

    def select(self, mailbox_ref, readonly=False):
        name = mailbox_ref.strip('"')
        if name not in self._messages:
            return "NO", [b"no such folder"]
        self._selected = name
        return "OK", [str(len(self._messages[name])).encode()]

    def uid(self, command, *args):
        if self._selected is None:
            return "NO", [b"nothing selected"]
        messages = self._messages[self._selected]
        if command == "search":
            criteria = [a.decode() if isinstance(a, bytes) else str(a) for a in args[1:]]
            if len(criteria) >= 3 and criteria[-3].upper() == "HEADER":
                header = criteria[-2].lower().encode()
                needle = criteria[-1].strip("<>").lower().encode()
                found = [
                    i for i, raw in enumerate(messages, start=1)
                    if any(line.lower().startswith(header + b":") and needle in line.lower()
                           for line in raw.split(b"\n"))
                ]
                return "OK", [" ".join(map(str, found)).encode()]
            return "OK", [" ".join(str(i) for i in range(1, len(messages) + 1)).encode()]
        if command == "fetch":
            uid_set, _spec = args[0], args[1]
            if isinstance(uid_set, bytes):
                uid_set = uid_set.decode()
            wanted = {int(x) for x in uid_set.split(",")}
            data = []
            for i, raw in enumerate(messages, start=1):
                if i in wanted:
                    # UID стабилен: 1000 + порядковый номер письма в папке.
                    uid = 1000 + i
                    data.append((f"{i} (UID {uid} RFC822 {{{len(raw)}}}".encode(), raw))
            data.append(b")")
            return "OK", data
        return "BAD", [b"unsupported uid command"]

    def append(self, mailbox, message, flagstr=None, dtstr=None):
        """APPEND: кладёт письмо в конец папки (создаёт её при необходимости)."""
        name = str(mailbox).strip('"')
        self._messages.setdefault(name, []).append(bytes(message))
        return "OK", [b""]

    def create(self, mailbox):
        self._messages.setdefault(str(mailbox).strip('"'), [])
        return "OK", [b""]

    def noop(self):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b"mock logout"]

    def close(self):
        return "OK", [b""]
