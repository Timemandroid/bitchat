"""Автопубликация по расписанию: мастер /new и управление /posts."""

import asyncio
from collections import defaultdict
from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Config
from ..db import ACTIVE, Database
from ..keyboards import IsAdmin, PostCb, chats_keyboard, confirm_keyboard, done_keyboard
from ..parsing import format_interval, format_ts, parse_interval, parse_runs, parse_start
from ..scheduler import PublishError, Scheduler
from .relay import COPYABLE

MAX_MESSAGES = 100  # ограничение copyMessages
NOT_COMMAND = ~F.text.startswith("/")  # команды вроде /posts работают и посреди мастера

router = Router(name="posts")
router.message.filter(F.chat.type == "private", IsAdmin())
router.callback_query.filter(IsAdmin())

# Части альбома приходят почти одновременно — без блокировки часть id потерялась бы.
_content_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class NewPost(StatesGroup):
    content = State()
    target = State()
    start = State()
    runs = State()
    interval = State()
    confirm = State()


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Сейчас нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменено.")


@router.callback_query(PostCb.filter(F.action == "abort"))
async def abort(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Создание расписания отменено.")
    await callback.answer()


@router.message(Command("new"))
async def new_post(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewPost.content)
    await state.update_data(message_ids=[], from_chat_id=message.chat.id)
    await message.answer(
        "<b>Шаг 1/5.</b> Пришлите или перешлите сюда то, что нужно публиковать: "
        "текст, фото, видео, альбом, файл, голосовое… Можно несколько сообщений подряд — "
        "они будут публиковаться вместе.\n\n"
        "⚠️ Не удаляйте эти сообщения из чата с ботом, пока расписание не закончится: "
        "бот копирует их отсюда.\n\n/cancel — отмена"
    )


@router.message(NewPost.content, NOT_COMMAND)
async def collect_content(message: Message, state: FSMContext) -> None:
    if message.content_type not in COPYABLE:
        await message.answer("Такое сообщение Telegram не даёт копировать — пришлите другое.")
        return

    async with _content_locks[message.from_user.id]:
        ids = (await state.get_data()).get("message_ids", [])
        if len(ids) >= MAX_MESSAGES:
            await message.answer(f"Больше {MAX_MESSAGES} сообщений в одном посте нельзя. Нажмите «Готово».")
            return
        ids = [*ids, message.message_id]
        await state.update_data(message_ids=ids)

    if len(ids) == 1:
        await message.answer(
            "Принято. Можно прислать ещё сообщения или нажать «Готово».", reply_markup=done_keyboard()
        )


@router.callback_query(NewPost.content, PostCb.filter(F.action == "done"))
async def content_done(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    ids = (await state.get_data()).get("message_ids", [])
    if not ids:
        await callback.answer("Сначала пришлите контент", show_alert=True)
        return
    chats = await db.list_chats()
    if not chats:
        await state.clear()
        await callback.message.edit_text(
            "Бот пока не состоит ни в одной группе. Добавьте его в группу и повторите /new."
        )
        await callback.answer()
        return

    await state.set_state(NewPost.target)
    await callback.message.edit_text(f"Сообщений в посте: {len(ids)}.")
    await callback.message.answer(
        "<b>Шаг 2/5.</b> Куда публиковать?",
        reply_markup=chats_keyboard(chats, lambda c: PostCb(action="target", chat_id=c.chat_id)),
    )
    await callback.answer()


@router.callback_query(NewPost.target, PostCb.filter(F.action == "target"))
async def pick_target(
    callback: CallbackQuery, callback_data: PostCb, state: FSMContext, db: Database, config: Config
) -> None:
    chat = await db.get_chat(callback_data.chat_id)
    if chat is None:
        await callback.answer("Эта группа пропала из списка", show_alert=True)
        return
    await state.update_data(target_chat_id=chat.chat_id, target_title=chat.title)
    await state.set_state(NewPost.start)
    now = datetime.now(config.tz)
    await callback.message.edit_text(f"Публиковать в: «{escape(chat.title)}».")
    await callback.message.answer(
        "<b>Шаг 3/5.</b> Когда первая публикация?\n"
        f"Сейчас у бота {now:%d.%m.%Y %H:%M} ({config.tz.key}).\n\n"
        "Примеры:\n"
        "• <code>сейчас</code>\n"
        "• <code>18:30</code> — сегодня, а если уже прошло — завтра\n"
        "• <code>28.10 18:30</code>\n"
        "• <code>28.10.2026 18:30</code>\n"
        "• <code>через 2ч</code>"
    )
    await callback.answer()


@router.message(NewPost.start, F.text, NOT_COMMAND)
async def got_start(message: Message, state: FSMContext, config: Config) -> None:
    try:
        start = parse_start(message.text, datetime.now(config.tz))
    except ValueError as e:
        await message.answer(f"❗ {e}")
        return
    await state.update_data(start_at=int(start.timestamp()))
    await state.set_state(NewPost.runs)
    await message.answer(
        f"Первая публикация: {start:%d.%m.%Y %H:%M}.\n\n"
        "<b>Шаг 4/5.</b> Сколько раз опубликовать? Например <code>5</code>. "
        "<code>1</code> — один раз, без повторов."
    )


@router.message(NewPost.runs, F.text, NOT_COMMAND)
async def got_runs(message: Message, state: FSMContext, config: Config) -> None:
    try:
        runs = parse_runs(message.text)
    except ValueError as e:
        await message.answer(f"❗ {e}")
        return
    await state.update_data(total_runs=runs)
    if runs == 1:
        await state.update_data(interval_seconds=0)
        await _ask_confirm(message, state, config)
        return
    await state.set_state(NewPost.interval)
    await message.answer(
        "<b>Шаг 5/5.</b> Как часто повторять?\n"
        "Примеры: <code>30м</code>, <code>2ч</code>, <code>1д</code>, <code>1д 12ч</code>"
    )


@router.message(NewPost.interval, F.text, NOT_COMMAND)
async def got_interval(message: Message, state: FSMContext, config: Config) -> None:
    try:
        interval = parse_interval(message.text)
    except ValueError as e:
        await message.answer(f"❗ {e}")
        return
    await state.update_data(interval_seconds=interval)
    await _ask_confirm(message, state, config)


async def _ask_confirm(message: Message, state: FSMContext, config: Config) -> None:
    data = await state.get_data()
    await state.set_state(NewPost.confirm)
    start, runs, interval = data["start_at"], data["total_runs"], data["interval_seconds"]
    lines = [
        "<b>Проверьте расписание:</b>",
        f"Куда: «{escape(data['target_title'])}»",
        f"Сообщений в посте: {len(data['message_ids'])}",
        f"Первая публикация: {format_ts(start, config.tz)}",
    ]
    if runs == 1:
        lines.append("Повторов нет — один раз.")
    else:
        lines.append(f"Всего публикаций: {runs}, каждые {format_interval(interval)}")
        lines.append(f"Последняя: {format_ts(start + (runs - 1) * interval, config.tz)}")
    await message.answer("\n".join(lines), reply_markup=confirm_keyboard())


@router.callback_query(NewPost.confirm, PostCb.filter(F.action == "save"))
async def save_post(callback: CallbackQuery, state: FSMContext, db: Database, scheduler: Scheduler) -> None:
    data = await state.get_data()
    await state.clear()
    post_id = await db.add_post(
        owner_id=callback.from_user.id,
        from_chat_id=data["from_chat_id"],
        message_ids=data["message_ids"],
        target_chat_id=data["target_chat_id"],
        start_at=data["start_at"],
        interval_seconds=data["interval_seconds"],
        total_runs=data["total_runs"],
    )
    scheduler.wake()
    await callback.message.edit_text(
        callback.message.html_text + f"\n\n✅ Расписание #{post_id} создано. Управление: /posts"
    )
    await callback.answer()


# --- список расписаний ---


async def _render_posts(db: Database, config: Config) -> tuple[str, InlineKeyboardMarkup | None]:
    posts = await db.list_active_posts()
    if not posts:
        return "Активных расписаний нет. /new — создать.", None

    lines = ["<b>Активные расписания:</b>"]
    kb = InlineKeyboardBuilder()
    for p in posts:
        where = escape(p.target_title or str(p.target_chat_id))
        lines.append(f"\n<b>#{p.id}</b> → «{where}»")
        lines.append(f"следующая: {format_ts(p.next_run_at, config.tz)}")
        if p.total_runs > 1:
            lines.append(
                f"выполнено {p.runs_done} из {p.total_runs}, каждые {format_interval(p.interval_seconds)}"
            )
        if p.last_error:
            lines.append(f"⚠️ последняя ошибка: {escape(p.last_error)}")
        kb.button(text=f"🚀 #{p.id} сейчас", callback_data=PostCb(action="now", id=p.id))
        kb.button(text=f"❌ #{p.id} отменить", callback_data=PostCb(action="stop", id=p.id))
    kb.adjust(2)
    return "\n".join(lines), kb.as_markup()


@router.message(Command("posts"))
async def list_posts(message: Message, db: Database, config: Config) -> None:
    text, kb = await _render_posts(db, config)
    await message.answer(text, reply_markup=kb)


@router.callback_query(PostCb.filter(F.action == "now"))
async def publish_now(callback: CallbackQuery, callback_data: PostCb, db: Database, scheduler: Scheduler) -> None:
    post = await db.get_post(callback_data.id)
    if post is None or post.status != ACTIVE:
        await callback.answer("Это расписание уже не активно", show_alert=True)
        return
    try:
        await scheduler.send(post)
    except (TelegramAPIError, PublishError) as e:
        await callback.answer(f"Не удалось: {e}"[:200], show_alert=True)
        return
    await callback.answer("Опубликовано вне очереди ✅ Счётчик повторов не изменился.", show_alert=True)


@router.callback_query(PostCb.filter(F.action == "stop"))
async def stop_post(callback: CallbackQuery, callback_data: PostCb, db: Database, config: Config) -> None:
    cancelled = await db.cancel_post(callback_data.id)
    text, kb = await _render_posts(db, config)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer(f"Расписание #{callback_data.id} отменено" if cancelled else "Уже не активно")
