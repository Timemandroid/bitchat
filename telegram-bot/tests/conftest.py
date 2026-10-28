import pytest

from bot.db import Database


class FakeBot:
    """Записывает вызовы Bot API вместо отправки их в Telegram."""

    def __init__(self):
        self.copy_message_calls = []
        self.copy_messages_calls = []
        self.sent = []
        self.fail_for: dict[int, Exception] = {}
        self.copy_messages_result = None

    async def copy_message(self, chat_id, from_chat_id, message_id):
        if chat_id in self.fail_for:
            raise self.fail_for[chat_id]
        self.copy_message_calls.append((chat_id, from_chat_id, message_id))

    async def copy_messages(self, chat_id, from_chat_id, message_ids):
        if chat_id in self.fail_for:
            raise self.fail_for[chat_id]
        self.copy_messages_calls.append((chat_id, from_chat_id, list(message_ids)))
        if self.copy_messages_result is not None:
            return self.copy_messages_result
        return list(message_ids)

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


@pytest.fixture
async def db(tmp_path):
    database = await Database.open(str(tmp_path / "test.db"))
    yield database
    await database.close()


@pytest.fixture
def bot():
    return FakeBot()
