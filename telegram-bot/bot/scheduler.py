"""Фоновая публикация постов по расписанию."""

import asyncio
import logging
import time
from html import escape

from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError

from .db import ACTIVE, DONE, Database, Post

log = logging.getLogger(__name__)

TICK_SECONDS = 15
TRANSIENT_RETRY_SECONDS = 60


class PublishError(Exception):
    pass


def next_slot(start_at: int, interval: int, now: int) -> int:
    """Ближайший слот расписания строго после `now`.

    Слоты считаются от времени первой публикации, поэтому повторные попытки
    и простой бота не сдвигают расписание, а пропущенные слоты не копятся.
    """
    if now < start_at:
        return start_at
    return start_at + ((now - start_at) // interval + 1) * interval


class Scheduler:
    def __init__(self, bot, db: Database, tick_seconds: int = TICK_SECONDS):
        self._bot = bot
        self._db = db
        self._tick_seconds = tick_seconds
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """Проверить расписания сразу, не дожидаясь следующего тика."""
        self._wake.set()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Ошибка в планировщике")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._tick_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def tick(self, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        for post in await self._db.due_posts(now):
            await self._run_post(post, now)

    async def send(self, post: Post) -> None:
        copied = await self._bot.copy_messages(
            chat_id=post.target_chat_id,
            from_chat_id=post.from_chat_id,
            message_ids=post.message_ids,
        )
        if not copied:
            raise PublishError("исходные сообщения не найдены — возможно, их удалили из чата с ботом")

    async def _run_post(self, post: Post, now: int) -> None:
        try:
            await self.send(post)
        except TelegramRetryAfter as e:
            await self._db.postpone_post(post.id, now + e.retry_after, str(e))
            return
        except (TelegramNetworkError, TelegramServerError) as e:
            log.warning("Пост #%s: временная ошибка, повтор через минуту: %s", post.id, e)
            await self._db.postpone_post(post.id, now + TRANSIENT_RETRY_SECONDS, str(e))
            return
        except (TelegramAPIError, PublishError) as e:
            error = str(e)
            log.warning("Пост #%s: не удалось опубликовать: %s", post.id, e)
        else:
            error = None

        runs_done = post.runs_done + 1
        if runs_done >= post.total_runs:
            status, next_run_at = DONE, post.next_run_at
        else:
            status, next_run_at = ACTIVE, next_slot(post.start_at, post.interval_seconds, now)
        await self._db.update_post_run(post.id, runs_done, next_run_at, status, error)

        where = escape(post.target_title or str(post.target_chat_id))
        if error:
            left = post.total_runs - runs_done
            await self._notify(
                post.owner_id,
                f"⚠️ Расписание #{post.id}: не удалось опубликовать в «{where}».\n"
                f"Ошибка: {escape(error)}\n"
                f"Осталось запусков: {left}. Отменить: /posts",
            )
        if status == DONE:
            await self._notify(post.owner_id, f"✅ Расписание #{post.id} («{where}») завершено.")

    async def _notify(self, user_id: int, text: str) -> None:
        try:
            await self._bot.send_message(user_id, text)
        except TelegramAPIError as e:
            log.warning("Не удалось отправить уведомление %s: %s", user_id, e)
