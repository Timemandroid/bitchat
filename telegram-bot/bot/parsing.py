"""Разбор того, что админ пишет в мастере расписания: время, интервал, количество."""

import re
from datetime import datetime, timedelta

MIN_INTERVAL = 60
MAX_INTERVAL = 365 * 86400
MAX_RUNS = 1000

_UNITS = {
    60: ("м", "мин", "минута", "минуты", "минут", "m", "min"),
    3600: ("ч", "час", "часа", "часов", "h"),
    86400: ("д", "дн", "день", "дня", "дней", "d"),
}
_UNIT_SECONDS = {name: seconds for seconds, names in _UNITS.items() for name in names}
_INTERVAL_PART = re.compile(r"(\d+)\s*([a-zа-яё]+)")

_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_DATE_TIME = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2})[:.](\d{2})$")
_RELATIVE = re.compile(r"^(?:через|\+)\s*(.+)$")


def parse_interval(text: str) -> int:
    """«30м», «2ч», «1д 12ч», «90 мин» → секунды."""
    text = text.strip().lower()
    total = 0
    pos = 0
    for match in _INTERVAL_PART.finditer(text):
        if text[pos:match.start()].strip():
            raise ValueError("Не понял интервал. Примеры: 30м, 2ч, 1д, 1д 12ч")
        unit = _UNIT_SECONDS.get(match.group(2))
        if unit is None:
            raise ValueError(f"Неизвестная единица «{match.group(2)}». Используйте м, ч или д")
        total += int(match.group(1)) * unit
        pos = match.end()
    if pos == 0 or text[pos:].strip():
        raise ValueError("Не понял интервал. Примеры: 30м, 2ч, 1д, 1д 12ч")
    if total < MIN_INTERVAL:
        raise ValueError("Интервал должен быть не меньше 1 минуты")
    if total > MAX_INTERVAL:
        raise ValueError("Интервал должен быть не больше 365 дней")
    return total


def parse_start(text: str, now: datetime) -> datetime:
    """Время первой публикации в часовом поясе `now`.

    Понимает: «сейчас», «18:30» (сегодня, а если прошло — завтра),
    «28.10 18:30», «28.10.2026 18:30», «через 2ч» / «+30м».
    """
    text = " ".join(text.strip().lower().split())
    tz = now.tzinfo

    if text in ("сейчас", "now"):
        return now

    relative = _RELATIVE.match(text)
    if relative:
        return now + timedelta(seconds=parse_interval(relative.group(1)))

    m = _TIME.match(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        result = _build(now.year, now.month, now.day, hour, minute, tz)
        if result <= now:
            tomorrow = result + timedelta(days=1)
            result = _build(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute, tz)
        return result

    m = _DATE_TIME.match(text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        hour, minute = int(m.group(4)), int(m.group(5))
        result = _build(year, month, day, hour, minute, tz)
        if not m.group(3) and result.date() < now.date():
            # «05.01 10:00», написанное в декабре, — это январь следующего года
            result = _build(year + 1, month, day, hour, minute, tz)
        if result <= now:
            raise ValueError("Это время уже прошло")
        return result

    raise ValueError("Не понял время. Примеры: сейчас, 18:30, 28.10 18:30, 28.10.2026 18:30, через 2ч")


def _build(year: int, month: int, day: int, hour: int, minute: int, tz) -> datetime:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tz)
    except ValueError:
        raise ValueError("Такой даты или времени не бывает") from None


def parse_runs(text: str) -> int:
    text = text.strip()
    if not text.isdigit():
        raise ValueError("Нужно целое число, например 5")
    runs = int(text)
    if not 1 <= runs <= MAX_RUNS:
        raise ValueError(f"Число повторов должно быть от 1 до {MAX_RUNS}")
    return runs


def format_interval(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{days} д" if days else "", f"{hours} ч" if hours else "", f"{minutes} мин" if minutes else ""]
    return " ".join(p for p in parts if p) or "0 мин"


def format_ts(ts: int, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m.%Y %H:%M")
