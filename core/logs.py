# Логирование: консоль + файл с ротацией (data/logs/y360-admin.log).
#
# Требование ТЗ: все действия системы (сбор, синхронизация, создание
# отделов, бэкап, пометка на удаление, восстановление) жёстко логируются.
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from core.config import ROOT

_LOG_DIR = ROOT / "data" / "logs"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5
_FMT = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_configured: set[str] = set()


def _utf8_stderr():
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    return sys.stderr


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Logger с файловым (ротация 10 МБ × 5) и консольным хендлерами."""
    logger = logging.getLogger(name)
    if name in _configured:
        return logger
    logger.setLevel(level)
    logger.propagate = False
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            _LOG_DIR / "y360-admin.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(_FMT)
        logger.addHandler(fh)
    except OSError:
        pass  # нет доступа к диску — остаётся консоль
    ch = logging.StreamHandler(_utf8_stderr())
    ch.setFormatter(_FMT)
    logger.addHandler(ch)
    _configured.add(name)
    return logger
