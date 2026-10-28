"""Учёт групп и каналов, в которых состоит бот."""

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message

from ..config import Config
from ..db import Database

log = logging.getLogger(__name__)

router = Router(name="groups")

GROUP_TYPES = {"group", "supergroup"}


def _is_in_chat(member) -> bool:
    if member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
        return True
    return member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False)


async def _notify_admins(bot: Bot, config: Config, text: str) -> None:
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError as e:
            log.warning("Не удалось уведомить админа %s: %s", admin_id, e)


@router.my_chat_member()
async def bot_membership_changed(event: ChatMemberUpdated, bot: Bot, db: Database, config: Config) -> None:
    chat = event.chat
    if chat.type == "private":
        return
    title = chat.title or str(chat.id)

    if not _is_in_chat(event.new_chat_member):
        if await db.get_chat(chat.id):
            await db.remove_chat(chat.id)
            await _notify_admins(bot, config, f"➖ Бот больше не состоит в «{escape(title)}».")
        return

    known = await db.get_chat(chat.id) is not None
    added_by_admin = event.from_user is not None and event.from_user.id in config.admin_ids
    if not known and not added_by_admin:
        # Бота может добавить к себе кто угодно — такие чаты в список не попадают.
        who = escape(event.from_user.full_name) if event.from_user else "кто-то"
        await _notify_admins(
            bot,
            config,
            f"ℹ️ {who} добавил бота в «{escape(title)}» (<code>{chat.id}</code>). "
            "В список групп она не добавлена. Если это ваша группа, напишите в ней /id со своего аккаунта.",
        )
        return

    await db.upsert_chat(chat.id, title, chat.type)
    if not known:
        await _notify_admins(
            bot,
            config,
            f"➕ Бот добавлен в «{escape(title)}» (<code>{chat.id}</code>).\n"
            "Теперь её можно выбрать в /route и /new.",
        )


@router.message(F.migrate_to_chat_id)
async def group_migrated(message: Message, db: Database) -> None:
    """Группа превратилась в супергруппу: у неё новый chat_id."""
    await db.migrate_chat(message.chat.id, message.migrate_to_chat_id)


@router.message(Command("id"), F.chat.type.in_(GROUP_TYPES))
async def group_id(message: Message, db: Database, config: Config) -> None:
    text = f"ID этого чата: <code>{message.chat.id}</code>"
    if message.from_user and message.from_user.id in config.admin_ids:
        await db.upsert_chat(message.chat.id, message.chat.title or str(message.chat.id), message.chat.type)
        text += "\nГруппа есть в списке бота — её можно выбрать в /route и /new."
    await message.reply(text)


@router.channel_post(Command("id"))
async def channel_id(message: Message) -> None:
    await message.reply(f"ID этого канала: <code>{message.chat.id}</code>")
