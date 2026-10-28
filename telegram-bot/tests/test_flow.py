"""Сквозная проверка: настоящий диспетчер и роутеры, вместо Telegram — фейковая сессия."""

import time
from datetime import datetime
from itertools import count
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    CopyMessages,
    EditMessageText,
    GetChatMember,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    MessageId,
    PhotoSize,
    Update,
    User,
)

from bot.__main__ import create_dispatcher
from bot.config import Config
from bot.handlers.relay import Relay
from bot.keyboards import PostCb, RouteCb
from bot.scheduler import Scheduler

ADMIN = 42
STRANGER = 7
GROUP_A, GROUP_B = -1001, -1002
TZ = ZoneInfo("Europe/Moscow")
BOT_USER = User(id=1, is_bot=True, first_name="bot", username="test_bot", can_read_all_group_messages=True)

DP = create_dispatcher()  # роутеры подключаются к диспетчеру только один раз за процесс
_ids = count(1)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(_ids),
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if isinstance(method, CopyMessages):
            return [MessageId(message_id=next(_ids)) for _ in method.message_ids]
        if isinstance(method, CopyMessage):
            return MessageId(message_id=next(_ids))
        if isinstance(method, GetChatMember):
            return ChatMemberAdministrator(
                user=BOT_USER,
                can_be_edited=False,
                is_anonymous=False,
                can_manage_chat=True,
                can_delete_messages=True,
                can_manage_video_chats=True,
                can_restrict_members=True,
                can_promote_members=False,
                can_change_info=True,
                can_invite_users=True,
                can_post_stories=False,
                can_edit_stories=False,
                can_delete_stories=False,
            )
        return True  # EditMessageText, AnswerCallbackQuery, SetMyCommands …

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError
        yield b""

    def of(self, kind):
        return [r for r in self.requests if isinstance(r, kind)]

    def texts(self):
        return [r.text for r in self.requests if isinstance(r, (SendMessage, EditMessageText))]


@pytest.fixture
async def env(db):
    session = FakeSession()
    bot = Bot("1:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    bot._me = BOT_USER
    config = Config(bot_token="1:TEST", admin_ids=frozenset({ADMIN}), tz=TZ, db_path=":memory:")
    scheduler = Scheduler(bot, db)
    data = dict(config=config, db=db, scheduler=scheduler, relay=Relay(bot, db, config.admin_ids, album_delay=0.01))

    async def feed(**update):
        await DP.feed_update(bot, Update(update_id=next(_ids), **update), **data)

    yield session, feed, db, scheduler
    DP.storage.storage.clear()  # состояния мастеров не должны переходить между тестами


def _user(user_id):
    return User(id=user_id, is_bot=False, first_name=f"u{user_id}")


def _dm(user_id, message_id=None, **content):
    return dict(
        message=Message(
            message_id=message_id or next(_ids),
            date=datetime.now(),
            chat=Chat(id=user_id, type="private"),
            from_user=_user(user_id),
            **content,
        )
    )


def _press(user_id, data):
    bot_msg = Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=user_id, type="private"), text="…")
    return dict(
        callback_query=CallbackQuery(
            id=str(next(_ids)), from_user=_user(user_id), chat_instance="ci", message=bot_msg, data=data
        )
    )


def _joined(chat_id, title, by):
    return dict(
        my_chat_member=ChatMemberUpdated(
            chat=Chat(id=chat_id, type="supergroup", title=title),
            from_user=_user(by),
            date=datetime.now(),
            old_chat_member=ChatMemberLeft(user=BOT_USER),
            new_chat_member=ChatMemberMember(user=BOT_USER),
        )
    )


def _group_msg(chat_id, message_id, **content):
    return dict(
        message=Message(
            message_id=message_id,
            date=datetime.now(),
            chat=Chat(id=chat_id, type="supergroup", title="g"),
            from_user=_user(STRANGER),
            **content,
        )
    )


async def test_stranger_gets_their_id_and_nothing_else(env):
    session, feed, db, _ = env
    await feed(**_dm(STRANGER, text="/new"))
    assert "приватный бот" in session.texts()[-1] and str(STRANGER) in session.texts()[-1]
    await feed(**_joined(GROUP_A, "Чужая", by=STRANGER))
    assert await db.list_chats() == []


async def test_schedule_wizard_creates_post_and_publishes(env):
    session, feed, db, scheduler = env
    await feed(**_joined(GROUP_A, "Группа А", by=ADMIN))
    assert [c.title for c in await db.list_chats()] == ["Группа А"]

    await feed(**_dm(ADMIN, text="/new"))
    photo = [PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]
    await feed(**_dm(ADMIN, message_id=500, photo=photo, caption="фото"))
    await feed(**_dm(ADMIN, message_id=501, text="подпись к посту"))
    await feed(**_press(ADMIN, PostCb(action="done").pack()))
    assert any("Шаг 2/5" in t for t in session.texts())

    await feed(**_press(ADMIN, PostCb(action="target", chat_id=GROUP_A).pack()))
    await feed(**_dm(ADMIN, text="завтра"))
    assert "Не понял время" in session.texts()[-1]

    # команда посреди мастера выполняется, а не принимается за ответ
    await feed(**_dm(ADMIN, text="/posts"))
    assert "Активных расписаний нет" in session.texts()[-1]

    await feed(**_dm(ADMIN, text="через 2ч"))
    await feed(**_dm(ADMIN, text="3"))
    await feed(**_dm(ADMIN, text="1ч"))
    assert "Проверьте расписание" in session.texts()[-1]
    await feed(**_press(ADMIN, PostCb(action="save").pack()))

    [post] = await db.list_active_posts()
    assert post.message_ids == [500, 501]
    assert (post.target_chat_id, post.from_chat_id, post.total_runs, post.interval_seconds) == (
        GROUP_A,
        ADMIN,
        3,
        3600,
    )
    assert abs(post.start_at - (time.time() + 7200)) < 60

    await scheduler.tick(now=post.start_at)
    [copy] = session.of(CopyMessages)
    assert (copy.chat_id, copy.from_chat_id, copy.message_ids) == (GROUP_A, ADMIN, [500, 501])

    await feed(**_press(ADMIN, PostCb(action="stop", id=post.id).pack()))
    assert await db.list_active_posts() == []


async def test_route_wizard_and_live_copy(env):
    session, feed, db, _ = env
    await feed(**_joined(GROUP_A, "Источник", by=ADMIN))
    await feed(**_joined(GROUP_B, "Получатель", by=ADMIN))

    await feed(**_dm(ADMIN, text="/route"))
    await feed(**_press(ADMIN, RouteCb(action="src", src=GROUP_A).pack()))
    await feed(**_press(ADMIN, RouteCb(action="dst", src=GROUP_A, dst=GROUP_B).pack()))
    assert "Связка создана" in session.texts()[-1]
    assert await db.targets_for(GROUP_A) == [GROUP_B]

    await feed(**_group_msg(GROUP_A, 77, text="новость"))
    [copy] = session.of(CopyMessage)
    assert (copy.chat_id, copy.from_chat_id, copy.message_id) == (GROUP_B, GROUP_A, 77)

    # /id в группе — это команда, а не сообщение для копирования
    await feed(**_group_msg(GROUP_A, 78, text="/id"))
    assert len(session.of(CopyMessage)) == 1
    assert any(str(GROUP_A) in t for t in session.texts())
    assert len(session.of(AnswerCallbackQuery)) == 2  # кнопки не «висят» с часиками
