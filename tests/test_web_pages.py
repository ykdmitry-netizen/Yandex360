"""Проверка веб-консоли NiceGUI: рендер всех 9 страниц + HTTP-маршруты.

Используется штатный симулятор пользователя NiceGUI (nicegui.testing.User,
ASGI-транспорт httpx) — он реально прогоняет серверные билдеры страниц и
ловит исключения, в отличие от простого GET.

Запуск: <repo>\\venv\\Scripts\\python.exe tests\\test_web_pages.py
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

# Корень репозитория: тесты лежат в <repo>/tests
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Тесты проверяют отрисовку страниц, а не вход: выключаем аутентификацию,
# иначе httpx уходит в редирект на ADFS и тест падает с TooManyRedirects.
import os  # noqa: E402

os.environ["AUTH_MODE"] = "none"

PAGES: list[tuple[str, str]] = [
    ("/", "состояние синхронизации и архивации"),
    ("/sync", "данные → план → карточки → события"),
    ("/mismatches", "AD ↔ Яндекс 360"),
    ("/users", "справочник и снимки ящиков"),
    ("/backup", "уволенные, снимки и очистка"),
    ("/restore", "просмотр снимков и возврат почты"),
    ("/retention", "правила по должностям"),
    ("/logs", "аудит, снимки и сообщения задач"),
    ("/settings", "окружение и параметры"),
]

RESULTS: list[tuple[bool, str, str]] = []
ERRORS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((bool(cond), name, detail))
    print(f"[{' OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class ErrorCollector(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            ERRORS.append(f"{record.name}: {record.getMessage()}")


async def run() -> None:
    import httpx
    import nicegui.storage
    from nicegui import app, core
    from nicegui.testing import User

    core.app.config.add_run_config(
        reload=False, title="Y360 Admin (тест)", viewport="", favicon=None,
        dark=True, language="ru", binding_refresh_interval=0.1,
        reconnect_timeout=3.0, message_history_length=1000,
        tailwind=True, prod_js=True, show_welcome_message=False,
    )
    nicegui.storage.set_storage_secret("simulated secret")

    logging.getLogger().addHandler(ErrorCollector())
    logging.getLogger("nicegui").setLevel(logging.WARNING)

    import web.main  # noqa: F401  — регистрация маршрутов @ui.page

    routes = sorted({r.path for r in app.routes
                     if str(getattr(r, "path", "")).startswith("/") and "{" not in r.path})
    check("Маршруты страниц зарегистрированы (9 + служебные)",
          all(path in routes for path, _ in PAGES),
          f"найдено: {sorted(p for p in routes if not p.startswith('/_nicegui'))}")

    transport = httpx.ASGITransport(app=core.app)
    async with core.app.router.lifespan_context(core.app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # --- HTTP-уровень: коды ответов ---
            for path, _ in PAGES:
                resp = await client.get(path, follow_redirects=True)
                check(f"HTTP GET {path} → 200 и заголовок X-Nicegui-Content: page",
                      resp.status_code == 200 and resp.headers.get("X-Nicegui-Content") == "page",
                      f"{resp.status_code}")

            resp = await client.get("/no-such-page", follow_redirects=False)
            check("HTTP GET /no-such-page → 404", resp.status_code == 404, f"{resp.status_code}")

            home = await client.get("/")
            assets = list(dict.fromkeys(re.findall(r'src="(/[^"]+)"', home.text)))
            broken = [(a, (await client.get(a)).status_code) for a in assets
                      if (await client.get(a)).status_code != 200]
            check(f"Статика NiceGUI отдаётся ({len(assets)} файлов)",
                  bool(assets) and not broken, f"битые: {broken}")

            # --- серверный рендер каждой страницы ---
            user = User(client)
            for path, marker in PAGES:
                try:
                    await user.open(path)
                    await user.should_see(marker, retries=6)
                    check(f"Рендер страницы {path}", True, f"виден подзаголовок «{marker}»")
                except Exception as exc:  # noqa: BLE001
                    check(f"Рендер страницы {path}", False, f"{type(exc).__name__}: {str(exc)[:300]}")

            # --- навигация и общие элементы каркаса ---
            try:
                await user.open("/")
                for label in ("Дашборд", "Мастер синхронизации", "Расхождения", "Сотрудники",
                              "Архивация", "Восстановление", "Сроки хранения", "Журнал", "Настройки"):
                    await user.should_see(label)
                check("Боковое меню: все 9 разделов", True)
            except Exception as exc:  # noqa: BLE001
                check("Боковое меню: все 9 разделов", False, f"{type(exc).__name__}: {str(exc)[:300]}")

            try:
                await user.open("/settings")
                for chip in ("AD:", "IMAP:", "API:"):
                    await user.should_see(chip)
                check("Индикаторы окружения в шапке (AD/IMAP/API)", True)
            except Exception as exc:  # noqa: BLE001
                check("Индикаторы окружения в шапке (AD/IMAP/API)", False,
                      f"{type(exc).__name__}: {str(exc)[:300]}")

            try:
                await user.open("/")
                await user.should_see("PostgreSQL · y360_admin")
                check("Подпись БД в меню", True)
            except Exception as exc:  # noqa: BLE001
                check("Подпись БД в меню", False, f"{type(exc).__name__}: {str(exc)[:300]}")

            # Регресс: панель снимка/аудита на мастере синхронизации (падала на tuple-курсоре)
            try:
                await user.open("/sync")
                await user.should_see("Снимок данных")
                await user.should_see("Журнал аудита")
                await user.should_not_see("Снимок недоступен")
                check("Мастер синхронизации: панели «Снимок данных» и «Журнал аудита» доступны", True)
            except Exception as exc:  # noqa: BLE001
                check("Мастер синхронизации: панели «Снимок данных» и «Журнал аудита» доступны", False,
                      f"{type(exc).__name__}: {str(exc)[:300]}")

    check("Ошибок уровня ERROR в логах приложения нет", not ERRORS,
          f"{len(ERRORS)}: {ERRORS[:3]}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(run())
    failed = [n for ok, n, _ in RESULTS if not ok]
    print("=" * 60)
    print(f"Проверок: {len(RESULTS)}; провалено: {len(failed)}")
    for n in failed:
        print(f"  FAIL: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
