"""Локальное хранилище бэкапов.

Для сценария «снимок уволенного»:
- каждый запуск пишет в новый каталог (никогда не перезаписывает прошлый снимок);
- рядом с MBOX формируется manifest.json;
- архив .tar.gz хешируется (sha256) для контроля целостности.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from datetime import datetime
from pathlib import Path

from core.config import get_settings
from core.logs import get_logger

logger = get_logger(__name__)

# Побочный индекс снимка: по строке JSON на письмо (папка, UID, смещение в MBOX).
INDEX_FILENAME = "index.jsonl"


class Storage:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe(login: str) -> str:
        return login.replace("/", "_").replace("\\", "_").replace("..", "_")

    def mailbox_dir(self, org_id: str, login: str, stamp: str | None = None) -> Path:
        """Каталог снимка. stamp — уникальная метка времени (YYYYMMDD_HHMMSS)."""
        stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.root / org_id / self._safe(login) / stamp

    def mbox_path(self, org_id: str, login: str, stamp: str | None = None) -> Path:
        return self.mailbox_dir(org_id, login, stamp) / "mail.mbox"

    def manifest_path(self, org_id: str, login: str, stamp: str | None = None) -> Path:
        return self.mailbox_dir(org_id, login, stamp) / "manifest.json"

    # ---------- Запись снимка ----------

    def prepare_snapshot(self, org_id: str, login: str) -> Path:
        """Создаёт новый каталог снимка (уникальная метка). Возвращает путь каталога."""
        d = self.mailbox_dir(org_id, login)
        d.mkdir(parents=True, exist_ok=False)  # exist_ok=False — гарантия уникальности
        return d

    def write_manifest(
        self,
        snapshot_dir: Path,
        *,
        org_id: str,
        login: str,
        reason: str,
        folders_count: int,
        messages: int,
        size_bytes: int,
        archive_name: str,
        archive_sha256: str,
        extra: dict | None = None,
    ) -> Path:
        manifest = {
            "org_id": org_id,
            "login": login,
            "reason": reason,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "folders_count": folders_count,
            "messages": messages,
            "size_bytes": size_bytes,
            "archive": archive_name,
            "archive_sha256": archive_sha256,
        }
        if extra:
            manifest.update(extra)
        mpath = snapshot_dir / "manifest.json"
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return mpath

    # ---------- Архив и хеш ----------

    def archive_snapshot(self, snapshot_dir: Path) -> tuple[Path, str]:
        """Пишет .tar.gz снимка и возвращает (путь_архива, sha256)."""
        archive_path = snapshot_dir.with_suffix(".tar.gz")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(snapshot_dir, arcname=snapshot_dir.name)
        sha256 = sha256_of_file(archive_path)
        return archive_path, sha256

    # ---------- Просмотр ----------

    def list_backups(self, org_id: str, login: str) -> list[Path]:
        base = self.root / org_id / self._safe(login)
        if not base.exists():
            return []
        return sorted(p for p in base.iterdir() if p.is_dir())

    def read_manifest(self, snapshot_dir: Path) -> dict | None:
        mp = snapshot_dir / "manifest.json"
        if not mp.exists():
            return None
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def verify_snapshot(self, snapshot_dir: Path) -> bool:
        """Проверяет sha256 архива против manifest.json."""
        manifest = self.read_manifest(snapshot_dir)
        if not manifest:
            return False
        archive_path = snapshot_dir.with_suffix(".tar.gz")
        if not archive_path.exists():
            return False
        actual = sha256_of_file(archive_path)
        return actual == manifest.get("archive_sha256")

    def cleanup_older_than(self, keep_days: int) -> int:
        """Удаляет снимки старше keep_days дней, возвращает число удалённых."""
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=keep_days)
        removed = 0
        for org_dir in self.root.iterdir():
            if not org_dir.is_dir():
                continue
            for login_dir in org_dir.iterdir():
                if not login_dir.is_dir():
                    continue
                for snap_dir in login_dir.iterdir():
                    if snap_dir.is_dir():
                        try:
                            ts = datetime.strptime(snap_dir.name, "%Y%m%d_%H%M%S")
                        except ValueError:
                            continue
                        if ts < cutoff:
                            shutil.rmtree(snap_dir, ignore_errors=True)
                            snap_dir.with_suffix(".tar.gz").unlink(missing_ok=True)
                            removed += 1
        return removed


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_storage() -> Storage:
    cfg = get_settings()
    return Storage(cfg.backup_root)
