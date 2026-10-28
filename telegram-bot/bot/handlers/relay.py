"""Живое копирование: новые сообщения из группы-источника уходят в группы-получатели."""

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from html import escape

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import Message

from ..db import Database

log = logging.getLogger(__name__)

# Всё, что Telegram позволяет боту скопировать. Служебные сообщения
# (вход участников, закрепы и т. п.) копировать нельзя — их пропускаем.
COPYABLE = frozenset(
    {
        ContentType.TEXT,
        ContentType.PHOTO,
        ContentType.VIDEO,
        ContentType.ANIMATION,
        ContentType.AUDIO,
        ContentType.DOCUMENT,
        ContentType.VOICE,
        ContentType.VIDEO_NOTE,
        ContentType.STICKER,
        ContentType.CONTACT,
        ContentType.LOCATION,
        ContentType.VENUE,
        ContentType.POLL,
        ContentType.DICE,
    }
)

ALBUM_DELAY = 1.5
ERROR_NOTIFY_COOLDOWN = 600

router = Router(name="relay")


class AlbumBuffer:
    """Собирает части альбома (одинаковый media_group_id), пока они идут подряд."""

    def __init__(self, delay: float, on_ready: Callable[[int, list[int]], Awaitable[None]]):
        self._delay = delay
        self._on_ready = on_ready
        self._ids: dict[tuple[int, str], list[int]] = {}
        self._timers: dict[tuple[int, str], asyncio.Task] = {}
        self._flushing: set[asyncio.Task] = set()

    def add(self, chat_id: int, media_group_id: str, message_id: int) -> None:
        key = (chat_id, media_group_id)
        self._ids.setdefault(key, []).append(message_id)
        timer = self._timers.get(key)
        if timer:
            timer.cancel()
        self._timers[key] = asyncio.create_task(self._flush_later(key))

    async def _flush_later(self, key: tuple[int, str]) -> None:
        await asyncio.sleep(self._delay)
        task = self._timers.pop(key)
        self._flushing.add(task)
        try:
            await self._on_ready(key[0], sorted(self._ids.pop(key)))
        except Exception:
            log.exception("Не удалось скопировать альбом")
        finally:
            self._flushing.discard(task)


class Relay:
    def __init__(self, bot, db: Database, admin_ids: frozenset[int], album_delay: float = ALBUM_DELAY):
        self._bot = bot
        self._db = db
        self._admin_ids = admin_ids
        self._albums = AlbumBuffer(album_delay, self.copy)
        # по одной очереди на получателя, чтобы сообщения приходили в исходном порядке
        self._target_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_error_notice: dict[tuple[int, str], float] = {}

    async def handle(self, message: Message) -> None:
        if message.content_type not in COPYABLE:
            return
        if message.media_group_id:
            self._albums.add(message.chat.id, message.media_group_id, message.message_id)
        else:
            await self.copy(message.chat.id, [message.message_id])

    async def copy(self, source_chat_id: int, message_ids: list[int]) -> None:
        for target in await self._db.targets_for(source_chat_id):
            async with self._target_locks[target]:
                try:
                    await self._copy_to(target, source_chat_id, message_ids)
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                    try:
                        await self._copy_to(target, source_chat_id, message_ids)
                    except TelegramAPIError as e2:
                        await self._report(source_chat_id, target, e2)
                except TelegramAPIError as e:
                    await self._report(source_chat_id, target, e)

    async def _copy_to(self, target: int, source: int, message_ids: list[int]) -> None:
        if len(message_ids) == 1:
            await self._bot.copy_message(chat_id=target, from_chat_id=source, message_id=message_ids[0])
        else:
            await self._bot.copy_messages(chat_id=target, from_chat_id=source, message_ids=message_ids)

    async def _report(self, source: int, target: int, error: TelegramAPIError) -> None:
        log.warning("Копирование %s → %s не удалось: %s", source, target, error)
        key = (target, error.message)
        now = time.monotonic()
        if now - self._last_error_notice.get(key, -ERROR_NOTIFY_COOLDOWN) < ERROR_NOTIFY_COOLDOWN:
            return
        self._last_error_notice[key] = now

        source_chat = await self._db.get_chat(source)
        target_chat = await self._db.get_chat(target)
        text = (
            "⚠️ Не удалось скопировать сообщение\n"
            f"из «{escape(source_chat.title if source_chat else str(source))}» "
            f"в «{escape(target_chat.title if target_chat else str(target))}».\n"
            f"Ошибка: {escape(error.message)}\n\n"
            "Проверьте, что бот состоит в группе-получателе и может в ней писать. "
            "Повторные такие ошибки не чаще раза в 10 минут."
        )
        for admin_id in self._admin_ids:
            try:
                await self._bot.send_message(admin_id, text)
            except TelegramAPIError:
                pass


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def from_group(message: Message, relay: Relay) -> None:
    await relay.handle(message)


@router.channel_post()
async def from_channel(message: Message, relay: Relay) -> None:
    await relay.handle(message)
