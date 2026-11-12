"""Подключение бота к чату Битрикс24.

    python -m app.bitrix_setup chats            показать групповые чаты и их номера
    python -m app.bitrix_setup connect chat123  создать бота, добавить его в чат, отправить проверку
    python -m app.bitrix_setup test             отправить проверочное сообщение ещё раз

Вебхуку Битрикс24 нужны права crm, im и imbot.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys

from .bitrix import Bitrix, BitrixError, is_open_line
from .config import Settings

DEFAULT_CODE = "reel_bot"


def load_bot(settings: Settings) -> dict:
    try:
        data = json.loads(settings.bot_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_bot(settings: Settings, data: dict) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.bot_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        settings.bot_file.chmod(0o600)
    except OSError:
        pass


def _need_bitrix(settings: Settings) -> Bitrix | None:
    b = Bitrix(settings)
    if not b.enabled:
        print("В .env не задан BITRIX_WEBHOOK_URL.")
        return None
    return b


def cmd_chats(settings: Settings) -> int:
    b = _need_bitrix(settings)
    if not b:
        return 1
    try:
        chats = b.recent_chats()
    except BitrixError as e:
        print(f"Список чатов не получен: {e}")
        return 1
    if not chats:
        print("Групповых чатов не найдено. Бот видит только чаты, где состоит владелец вебхука.")
        return 0
    print("Групповые чаты владельца вебхука:")
    for c in chats:
        print(f"  {c.get('id'):<12} {c.get('title', '')}")
    print("\nДальше: python -m app.bitrix_setup connect chatНОМЕР")
    return 0


def cmd_connect(settings: Settings, dialog_id: str, code: str) -> int:
    b = _need_bitrix(settings)
    if not b:
        return 1
    dialog_id = dialog_id.strip()
    if not dialog_id.startswith("chat") or not dialog_id[4:].isdigit():
        print("Номер чата пишется так: chat123. Список чатов: python -m app.bitrix_setup chats")
        return 1

    # 1. Это должен быть внутренний чат, а не переписка с клиентом
    try:
        info = b.dialog_info(dialog_id)
    except BitrixError as e:
        print(f"Чат {dialog_id} не открылся: {e}")
        print("Проверьте номер чата и то, что владелец вебхука в нём состоит.")
        return 1
    if is_open_line(info):
        print(f"{dialog_id} это чат открытой линии, то есть переписка с клиентом.")
        print("Сообщение бота туда может уйти клиенту в Instagram. Выберите внутренний чат менеджеров.")
        return 1
    title = info.get("name") or info.get("title") or dialog_id
    print(f"Чат: {title} ({dialog_id})")

    # 2. Бот. Токен придумываем сами и храним: повторная регистрация его не меняет
    bot = load_bot(settings)
    if bot.get("code") != code or not bot.get("bot_token"):
        bot = {"code": code, "bot_token": secrets.token_urlsafe(24)}
    try:
        bot_id = b.bot_register(code, bot["bot_token"], settings.bitrix_bot_name)
    except BitrixError as e:
        print(f"Бот не зарегистрирован: {e}")
        print("Проверьте, что у вебхука есть право imbot и что тариф Битрикс24 разрешает REST.")
        return 1
    if not bot_id:
        print("Битрикс24 не вернул номер бота.")
        return 1
    bot.update(bot_id=bot_id, name=settings.bitrix_bot_name)
    save_bot(settings, bot)
    print(f"Бот «{settings.bitrix_bot_name}» готов, номер {bot_id}")

    # 3. Добавляем бота в чат от имени владельца вебхука
    try:
        b.chat_add_users(int(dialog_id[4:]), [bot_id])
        print("Бот добавлен в чат")
    except BitrixError as e:
        print(f"Добавить бота в чат автоматически не вышло: {e}")

    # 4. Проверочное сообщение
    try:
        b.bot_send(bot_id, bot["bot_token"], dialog_id,
                   "Бот подключён. Сюда будут приходить рилсы из Instagram Direct и бриф о товаре.")
    except BitrixError as e:
        print(f"Проверочное сообщение не отправлено: {e}")
        print(f"Добавьте бота в чат вручную: в чате «Добавить участников», найдите «{settings.bitrix_bot_name}»,")
        print("затем повторите эту команду.")
        return 1
    bot["dialog_id"] = dialog_id
    save_bot(settings, bot)
    print("Проверочное сообщение отправлено. Бот подключён к чату.")
    return 0


def cmd_test(settings: Settings) -> int:
    b = _need_bitrix(settings)
    if not b:
        return 1
    bot = load_bot(settings)
    if not bot.get("bot_id") or not bot.get("dialog_id"):
        print("Бот ещё не подключён. Сначала: python -m app.bitrix_setup connect chatНОМЕР")
        return 1
    try:
        b.bot_send(int(bot["bot_id"]), bot["bot_token"], bot["dialog_id"], "Проверка связи: бот на месте.")
    except BitrixError as e:
        print(f"Сообщение не отправлено: {e}")
        return 1
    print(f"Сообщение отправлено в {bot['dialog_id']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Подключение бота к чату Битрикс24")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chats", help="показать групповые чаты")
    c = sub.add_parser("connect", help="подключить бота к чату")
    c.add_argument("dialog_id", help="номер чата вида chat123")
    c.add_argument("--code", default=DEFAULT_CODE, help="внутренний код бота")
    sub.add_parser("test", help="отправить проверочное сообщение")
    a = p.parse_args(argv)
    s = Settings.from_env()
    if a.cmd == "chats":
        return cmd_chats(s)
    if a.cmd == "connect":
        return cmd_connect(s, a.dialog_id, a.code)
    return cmd_test(s)


if __name__ == "__main__":
    sys.exit(main())
