"""Запуск: python -m bot"""

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from .config import load_config
from .db import Database
from .handlers import common, groups, posts, relay, routes
from .scheduler import Scheduler

COMMANDS = [
    BotCommand(command="new", description="Новый пост по расписанию"),
    BotCommand(command="posts", description="Активные расписания"),
    BotCommand(command="route", description="Новая связка копирования"),
    BotCommand(command="routes", description="Список связок"),
    BotCommand(command="chats", description="Группы бота"),
    BotCommand(command="cancel", description="Прервать действие"),
    BotCommand(command="help", description="Помощь"),
]


def create_dispatcher(**workflow_data) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), **workflow_data)
    # порядок важен: команды в группах и личке разбираются раньше, чем копирование
    dp.include_routers(
        groups.router,
        common.admin_router,
        routes.router,
        posts.router,
        common.fallback_router,
        relay.router,
    )
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    db = await Database.open(config.db_path)
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    scheduler = Scheduler(bot, db)

    dp = create_dispatcher(
        config=config,
        db=db,
        scheduler=scheduler,
        relay=relay.Relay(bot, db, config.admin_ids),
    )

    scheduler_task = asyncio.create_task(scheduler.run())
    try:
        await bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError:
        raise SystemExit("Telegram не принял BOT_TOKEN. Проверьте токен в .env (его выдаёт @BotFather).") from None
    finally:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
