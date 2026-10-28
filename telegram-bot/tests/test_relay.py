import asyncio
from datetime import datetime

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import CopyMessage
from aiogram.types import Chat, Message, PhotoSize, User

from bot.handlers.relay import Relay

SOURCE, TARGET_B, TARGET_C, TARGET_OFF = -1, -2, -3, -4
ADMINS = frozenset({42})


async def _setup(db):
    for chat_id, title in [(SOURCE, "A"), (TARGET_B, "B"), (TARGET_C, "C"), (TARGET_OFF, "D")]:
        await db.upsert_chat(chat_id, title, "supergroup")
    await db.add_route(SOURCE, TARGET_B)
    await db.add_route(SOURCE, TARGET_C)
    await db.add_route(SOURCE, TARGET_OFF)
    off = [r for r in await db.list_routes() if r.target_chat_id == TARGET_OFF][0]
    await db.toggle_route(off.id)


def _msg(message_id, chat_id=SOURCE, **content):
    return Message(
        message_id=message_id,
        date=datetime(2026, 10, 28),
        chat=Chat(id=chat_id, type="supergroup", title="A"),
        **content,
    )


def _photo(message_id, group):
    photo = [PhotoSize(file_id=f"f{message_id}", file_unique_id=f"u{message_id}", width=1, height=1)]
    return _msg(message_id, photo=photo, media_group_id=group)


async def test_single_message_goes_to_every_enabled_target(db, bot):
    await _setup(db)
    relay = Relay(bot, db, ADMINS)
    await relay.handle(_msg(10, text="привет"))
    assert sorted(bot.copy_message_calls) == sorted([(TARGET_B, SOURCE, 10), (TARGET_C, SOURCE, 10)])


async def test_album_is_copied_as_one_album(db, bot):
    await _setup(db)
    relay = Relay(bot, db, ADMINS, album_delay=0.05)
    await asyncio.gather(*(relay.handle(_photo(i, "g1")) for i in (12, 10, 11)))
    assert bot.copy_messages_calls == []  # ждём, пока придут все части
    await asyncio.sleep(0.2)
    assert sorted(bot.copy_messages_calls) == sorted(
        [(TARGET_B, SOURCE, [10, 11, 12]), (TARGET_C, SOURCE, [10, 11, 12])]
    )
    assert bot.copy_message_calls == []


async def test_service_messages_are_skipped(db, bot):
    await _setup(db)
    relay = Relay(bot, db, ADMINS)
    await relay.handle(_msg(10, new_chat_members=[User(id=1, is_bot=False, first_name="Иван")]))
    assert bot.copy_message_calls == []


async def test_unrouted_chat_is_ignored(db, bot):
    await _setup(db)
    relay = Relay(bot, db, ADMINS)
    await relay.handle(_msg(10, chat_id=TARGET_B, text="привет"))
    assert bot.copy_message_calls == []


async def test_failing_target_does_not_block_others_and_notifies_once(db, bot):
    await _setup(db)
    relay = Relay(bot, db, ADMINS)
    error = TelegramForbiddenError(CopyMessage(chat_id=TARGET_B, from_chat_id=SOURCE, message_id=1), "bot was kicked")
    bot.fail_for[TARGET_B] = error

    await relay.handle(_msg(10, text="раз"))
    await relay.handle(_msg(11, text="два"))

    assert bot.copy_message_calls == [(TARGET_C, SOURCE, 10), (TARGET_C, SOURCE, 11)]
    assert len(bot.sent) == 1
    admin_id, text = bot.sent[0]
    assert admin_id == 42 and "«A»" in text and "«B»" in text and "bot was kicked" in text


async def test_chat_migration_keeps_routes(db, bot):
    await _setup(db)
    await db.add_post(42, 42, [1], TARGET_B, 1000, 0, 1)
    await db.migrate_chat(TARGET_B, -1002)
    assert await db.get_chat(TARGET_B) is None
    assert (await db.get_chat(-1002)).title == "B"
    assert -1002 in await db.targets_for(SOURCE)
    assert (await db.list_active_posts())[0].target_chat_id == -1002
