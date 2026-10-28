"""Связки копирования «источник → получатель»: /route, /routes."""

from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Chat, Database
from ..keyboards import IsAdmin, RouteCb, chats_keyboard

router = Router(name="routes")
router.message.filter(F.chat.type == "private", IsAdmin())
router.callback_query.filter(IsAdmin())


@router.message(Command("route"))
async def new_route(message: Message, db: Database) -> None:
    chats = await db.list_chats()
    if len(chats) < 2:
        await message.answer(
            "Для связки нужны минимум две группы, а бот знает "
            f"{len(chats)}. Добавьте бота в обе группы (или напишите в группе /id со своего аккаунта) "
            "и повторите /route."
        )
        return
    await message.answer(
        "Откуда копировать? Выберите группу-источник:",
        reply_markup=chats_keyboard(chats, lambda c: RouteCb(action="src", src=c.chat_id)),
    )


@router.callback_query(RouteCb.filter(F.action == "src"))
async def pick_source(callback: CallbackQuery, callback_data: RouteCb, db: Database) -> None:
    source = await db.get_chat(callback_data.src)
    if source is None:
        await callback.answer("Эта группа пропала из списка", show_alert=True)
        return
    targets = [c for c in await db.list_chats() if c.chat_id != source.chat_id]
    await callback.message.edit_text(
        f"Источник: «{escape(source.title)}».\nКуда копировать? Выберите получатель:",
        reply_markup=chats_keyboard(targets, lambda c: RouteCb(action="dst", src=source.chat_id, dst=c.chat_id)),
    )
    await callback.answer()


@router.callback_query(RouteCb.filter(F.action == "dst"))
async def pick_target(callback: CallbackQuery, callback_data: RouteCb, db: Database, bot: Bot) -> None:
    source = await db.get_chat(callback_data.src)
    target = await db.get_chat(callback_data.dst)
    if source is None or target is None:
        await callback.answer("Группа пропала из списка", show_alert=True)
        return

    if not await db.add_route(source.chat_id, target.chat_id):
        await callback.message.edit_text("Такая связка уже есть. Список: /routes")
        await callback.answer()
        return

    text = (
        f"✅ Связка создана: «{escape(source.title)}» → «{escape(target.title)}».\n"
        "Все новые сообщения из источника будут копироваться в получатель."
    )
    warnings = await _access_warnings(bot, source, target)
    if warnings:
        text += "\n\n" + "\n\n".join(warnings)
    await callback.message.edit_text(text)
    await callback.answer()


async def _access_warnings(bot: Bot, source: Chat, target: Chat) -> list[str]:
    """Проверяем, увидит ли бот сообщения источника и сможет ли писать в получатель."""
    warnings = []
    try:
        me = await bot.me()
        source_member = await bot.get_chat_member(source.chat_id, me.id)
        target_member = await bot.get_chat_member(target.chat_id, me.id)
    except TelegramAPIError as e:
        return [f"⚠️ Не удалось проверить права бота: {escape(e.message)}"]

    source_admin = source_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    if source.type == "channel" and not source_admin:
        warnings.append("⚠️ В канале-источнике бот должен быть администратором, иначе он не видит посты.")
    elif source.type != "channel" and not source_admin and not me.can_read_all_group_messages:
        warnings.append(
            "⚠️ Сейчас бот не видит обычные сообщения в источнике: у него включён privacy mode "
            "и он не администратор группы. Сделайте бота администратором группы-источника "
            "или в @BotFather: /setprivacy → Disable, затем удалите бота из группы и добавьте снова."
        )

    if target.type == "channel" and not getattr(target_member, "can_post_messages", False):
        warnings.append("⚠️ В канале-получателе бот должен быть администратором с правом публикации.")
    elif target_member.status == ChatMemberStatus.RESTRICTED and not getattr(
        target_member, "can_send_messages", True
    ):
        warnings.append("⚠️ В группе-получателе боту запрещено писать сообщения.")
    return warnings


async def _render_routes(db: Database) -> tuple[str, InlineKeyboardMarkup | None]:
    routes = await db.list_routes()
    if not routes:
        return "Связок пока нет. /route — создать.", None

    lines = ["Связки копирования:"]
    kb = InlineKeyboardBuilder()
    for r in routes:
        state = "✅" if r.enabled else "⏸"
        source = escape(r.source_title or str(r.source_chat_id))
        target = escape(r.target_title or str(r.target_chat_id))
        lines.append(f"{state} #{r.id}: «{source}» → «{target}»")
        kb.button(
            text=f"{'⏸ Выкл' if r.enabled else '▶️ Вкл'} #{r.id}",
            callback_data=RouteCb(action="toggle", id=r.id),
        )
        kb.button(text=f"🗑 Удалить #{r.id}", callback_data=RouteCb(action="del", id=r.id))
    kb.adjust(2)
    return "\n".join(lines), kb.as_markup()


@router.message(Command("routes"))
async def list_routes(message: Message, db: Database) -> None:
    text, kb = await _render_routes(db)
    await message.answer(text, reply_markup=kb)


@router.callback_query(RouteCb.filter(F.action.in_({"toggle", "del"})))
async def change_route(callback: CallbackQuery, callback_data: RouteCb, db: Database) -> None:
    if callback_data.action == "toggle":
        await db.toggle_route(callback_data.id)
    else:
        await db.delete_route(callback_data.id)
    text, kb = await _render_routes(db)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()
