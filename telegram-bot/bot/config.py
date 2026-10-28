"""Настройки бота из переменных окружения / файла .env."""

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    tz: ZoneInfo
    db_path: str


def _parse_admin_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            raise SystemExit(f"ADMIN_IDS: «{part}» не похоже на числовой ID пользователя") from None
    return frozenset(ids)


def load_config() -> Config:
    load_dotenv()

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен от @BotFather.")

    admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    if not admin_ids:
        raise SystemExit(
            "Не задан ADMIN_IDS. Впишите в .env свой Telegram ID "
            "(узнать его можно, написав боту /start — он покажет ID)."
        )

    tz_name = os.getenv("TIMEZONE", "Europe/Moscow").strip()
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f"TIMEZONE: неизвестный часовой пояс «{tz_name}» (пример: Europe/Moscow)") from None

    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        tz=tz,
        db_path=os.getenv("DB_PATH", "bot.db").strip() or "bot.db",
    )
