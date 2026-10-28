from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import CopyMessages

from bot.db import ACTIVE, CANCELLED, DONE
from bot.scheduler import Scheduler, next_slot

OWNER = 42
DM = 42
TARGET = -100


def _method():
    return CopyMessages(chat_id=TARGET, from_chat_id=DM, message_ids=[1])


async def _add(db, start_at=1000, interval=600, runs=3, ids=(7, 5, 6)):
    await db.upsert_chat(TARGET, "Целевая", "supergroup")
    return await db.add_post(OWNER, DM, list(ids), TARGET, start_at, interval, runs)


def test_next_slot():
    assert next_slot(1000, 600, 500) == 1000
    assert next_slot(1000, 600, 1000) == 1600
    assert next_slot(1000, 600, 1599) == 1600
    assert next_slot(1000, 600, 1600) == 2200
    assert next_slot(1000, 600, 3000) == 3400


async def test_publishes_on_time_and_finishes_after_all_runs(db, bot):
    post_id = await _add(db)
    scheduler = Scheduler(bot, db)

    await scheduler.tick(now=999)
    assert bot.copy_messages_calls == []

    await scheduler.tick(now=1000)
    # id сортируются: copyMessages требует возрастающий порядок
    assert bot.copy_messages_calls == [(TARGET, DM, [5, 6, 7])]
    post = await db.get_post(post_id)
    assert (post.runs_done, post.next_run_at, post.status) == (1, 1600, ACTIVE)

    await scheduler.tick(now=1300)
    assert len(bot.copy_messages_calls) == 1

    await scheduler.tick(now=1605)
    await scheduler.tick(now=2200)
    assert len(bot.copy_messages_calls) == 3
    post = await db.get_post(post_id)
    assert (post.runs_done, post.status) == (3, DONE)
    assert any("завершено" in text for _, text in bot.sent)

    await scheduler.tick(now=99999)
    assert len(bot.copy_messages_calls) == 3


async def test_single_run_post(db, bot):
    post_id = await _add(db, interval=0, runs=1)
    await Scheduler(bot, db).tick(now=1000)
    post = await db.get_post(post_id)
    assert (post.runs_done, post.status) == (1, DONE)
    assert len(bot.copy_messages_calls) == 1


async def test_downtime_publishes_once_and_skips_missed_slots(db, bot):
    post_id = await _add(db, runs=5)
    await Scheduler(bot, db).tick(now=3000)  # бот лежал, пропущено 4 слота
    assert len(bot.copy_messages_calls) == 1
    post = await db.get_post(post_id)
    assert (post.runs_done, post.next_run_at) == (1, 3400)


async def test_permanent_error_uses_up_slot_and_notifies_owner(db, bot):
    post_id = await _add(db)
    bot.fail_for[TARGET] = TelegramBadRequest(_method(), "chat not found")
    await Scheduler(bot, db).tick(now=1000)

    post = await db.get_post(post_id)
    assert (post.runs_done, post.next_run_at, post.status) == (1, 1600, ACTIVE)
    assert "chat not found" in post.last_error
    assert len(bot.sent) == 1 and bot.sent[0][0] == OWNER and "не удалось" in bot.sent[0][1]


async def test_transient_error_retries_without_using_slot_or_drifting(db, bot):
    post_id = await _add(db)
    scheduler = Scheduler(bot, db)

    bot.fail_for[TARGET] = TelegramNetworkError(_method(), "timeout")
    await scheduler.tick(now=1000)
    post = await db.get_post(post_id)
    assert (post.runs_done, post.next_run_at) == (0, 1060)
    assert bot.sent == []

    del bot.fail_for[TARGET]
    await scheduler.tick(now=1060)
    post = await db.get_post(post_id)
    # следующий слот — от исходного расписания, а не от времени повтора
    assert (post.runs_done, post.next_run_at, post.last_error) == (1, 1600, None)


async def test_flood_limit_waits_as_telegram_asks(db, bot):
    post_id = await _add(db)
    bot.fail_for[TARGET] = TelegramRetryAfter(_method(), "flood", retry_after=30)
    await Scheduler(bot, db).tick(now=1000)
    post = await db.get_post(post_id)
    assert (post.runs_done, post.next_run_at) == (0, 1030)


async def test_deleted_source_messages_are_reported(db, bot):
    post_id = await _add(db)
    bot.copy_messages_result = []
    await Scheduler(bot, db).tick(now=1000)
    post = await db.get_post(post_id)
    assert post.runs_done == 1
    assert "не найдены" in post.last_error


async def test_cancelled_post_is_not_published(db, bot):
    post_id = await _add(db)
    assert await db.cancel_post(post_id)
    assert not await db.cancel_post(post_id)
    await Scheduler(bot, db).tick(now=1000)
    assert bot.copy_messages_calls == []
    assert (await db.get_post(post_id)).status == CANCELLED
