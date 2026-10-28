"""Inline-клавиатуры, данные кнопок и фильтр администраторов."""

from collections.abc import Callable

from aiogram.filters import BaseFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Config
from .db import Chat


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, config: Config) -> bool:
        return event.from_user is not None and event.from_user.id in config.admin_ids


class RouteCb(CallbackData, prefix="rt"):
    action: str
    src: int = 0
    dst: int = 0
    id: int = 0


class PostCb(CallbackData, prefix="ps"):
    action: str
    chat_id: int = 0
    id: int = 0


def chat_title(chat: Chat) -> str:
    icon = "📢" if chat.type == "channel" else "👥"
    return f"{icon} {chat.title}"


def chats_keyboard(chats: list[Chat], make_data: Callable[[Chat], CallbackData]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for chat in chats:
        kb.button(text=chat_title(chat), callback_data=make_data(chat))
    kb.adjust(1)
    return kb.as_markup()


def done_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=PostCb(action="done"))
    kb.button(text="❌ Отмена", callback_data=PostCb(action="abort"))
    return kb.as_markup()


def confirm_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data=PostCb(action="save"))
    kb.button(text="❌ Отмена", callback_data=PostCb(action="abort"))
    return kb.as_markup()
