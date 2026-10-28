"""/start, /help, /chats и ответы на всё остальное в личке."""

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..config import Config
from ..db import Database
from ..keyboards import IsAdmin, chat_title

HELP = (
    "Я копирую сообщения между группами и публикую посты по расписанию.\n\n"
    "<b>Копирование</b>\n"
    "/route — новая связка «откуда → куда»\n"
    "/routes — список связок (вкл/выкл, удалить)\n\n"
    "<b>Расписание</b>\n"
    "/new — новый пост по расписанию\n"
    "/posts — активные расписания (опубликовать сейчас, отменить)\n\n"
    "<b>Прочее</b>\n"
    "/chats — группы и каналы, где состоит бот\n"
    "/cancel — прервать текущее действие\n"
    "В группе: /id — показать ID чата"
)

admin_router = Router(name="common-admin")
admin_router.message.filter(F.chat.type == "private", IsAdmin())

fallback_router = Router(name="common-fallback")
fallback_router.message.filter(F.chat.type == "private")


@admin_router.message(CommandStart())
@admin_router.message(Command("help"))
async def start(message: Message) -> None:
    await message.answer(HELP)


@admin_router.message(Command("chats"))
async def list_chats(message: Message, db: Database) -> None:
    chats = await db.list_chats()
    if not chats:
        await message.answer(
            "Бот пока не знает ни одной группы. Добавьте его в группу со своего аккаунта "
            "(или напишите в группе /id)."
        )
        return
    lines = ["Группы и каналы бота:"]
    lines += [f"{escape(chat_title(c))} — <code>{c.chat_id}</code>" for c in chats]
    await message.answer("\n".join(lines))


@fallback_router.message()
async def anything_else(message: Message, state: FSMContext, config: Config) -> None:
    if message.from_user is None or message.from_user.id not in config.admin_ids:
        user_id = message.from_user.id if message.from_user else "?"
        await message.answer(
            "Это приватный бот.\n"
            f"Ваш Telegram ID: <code>{user_id}</code> — если вы владелец, впишите его в ADMIN_IDS."
        )
        return
    if await state.get_state() is not None:
        await message.answer("Ответьте на вопрос выше (или нажмите кнопку). /cancel — прервать.")
    else:
        await message.answer("Не понял. /help — список команд.")


@fallback_router.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка уже не действует", show_alert=True)
