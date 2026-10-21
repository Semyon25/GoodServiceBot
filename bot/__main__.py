"""Точка входа: python -m bot"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from bot.config import ConfigError, load_config
from bot.db.connection import Database
from bot.db.migrations import migrate
from bot.db.repositories import Repositories
from bot.jobs import run_forever
from bot.services import Services
from bot.services.settings import Clock
from bot.telegram.handlers import build_router
from bot.telegram.middlewares import ActorMiddleware, PrivateChatMiddleware
from bot.telegram.notifier import Notifier

log = logging.getLogger("bot")

RETRY_SECONDS = 15
COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]


async def wait_for_telegram(bot: Bot, proxy_url: str | None) -> None:
    """Проверяет токен и связь с api.telegram.org; при сетевой ошибке повторяет попытки, а не падает."""
    while True:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("Telegram отклонил BOT_TOKEN — проверьте токен в .env (его выдаёт @BotFather).")
            raise SystemExit(2) from None
        except (TelegramNetworkError, ProxyError, ProxyConnectionError, ProxyTimeoutError) as exc:
            reason = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            hint = (
                "Проверьте настройки прокси PROXY_URL в .env."
                if proxy_url
                else "Проверьте интернет; если api.telegram.org недоступен из вашей сети, "
                "укажите прокси в PROXY_URL в .env (см. README)."
            )
            log.error("Нет связи с api.telegram.org: %s. %s Повтор через %s с.", reason, hint, RETRY_SECONDS)
            await asyncio.sleep(RETRY_SECONDS)
            continue
        log.info("Подключено к Telegram как @%s", me.username)
        return


async def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = Database(config.db_path)
    await db.connect()
    version = await migrate(db)
    log.info("База данных %s, версия схемы %s", config.db_path, version)

    clock = Clock(config.timezone)
    services = Services(Repositories(db), config.admin_ids, config.timezone, clock.now)

    session = AiohttpSession(proxy=config.proxy_url) if config.proxy_url else AiohttpSession()
    bot = Bot(config.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, services, config.admin_ids)

    dp = Dispatcher(storage=MemoryStorage(), services=services, notifier=notifier)
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(PrivateChatMiddleware())
        observer.outer_middleware(ActorMiddleware(services))
    dp.include_router(build_router())

    jobs: asyncio.Task | None = None
    try:
        await wait_for_telegram(bot, config.proxy_url)
        try:
            await bot.set_my_commands(COMMANDS)
        except TelegramNetworkError as exc:
            log.warning("Не удалось установить список команд: %s", exc.message)
        jobs = asyncio.create_task(run_forever(services, notifier, config.jobs_interval_seconds))
        log.info("Бот запущен")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if jobs:
            jobs.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await jobs
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
