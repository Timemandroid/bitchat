from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.parsing import format_interval, format_ts, parse_interval, parse_runs, parse_start

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 28, 15, 0, tzinfo=TZ)


@pytest.mark.parametrize(
    "text, seconds",
    [
        ("30м", 1800),
        ("2ч", 7200),
        ("1д", 86400),
        ("1д 12ч", 129600),
        ("1Д12Ч", 129600),
        ("90 мин", 5400),
        ("2 часа", 7200),
        ("3 дня", 259200),
        ("2h", 7200),
        ("1d 30m", 88200),
    ],
)
def test_parse_interval(text, seconds):
    assert parse_interval(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "5", "0м", "2 недели", "1д x", "x 1д", "400д"])
def test_parse_interval_rejects(text):
    with pytest.raises(ValueError):
        parse_interval(text)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("сейчас", NOW),
        ("Сейчас", NOW),
        ("18:30", datetime(2026, 10, 28, 18, 30, tzinfo=TZ)),
        ("18.30", datetime(2026, 10, 28, 18, 30, tzinfo=TZ)),
        # уже прошло сегодня — значит завтра
        ("14:00", datetime(2026, 10, 29, 14, 0, tzinfo=TZ)),
        ("15:00", datetime(2026, 10, 29, 15, 0, tzinfo=TZ)),
        ("28.10 18:30", datetime(2026, 10, 28, 18, 30, tzinfo=TZ)),
        ("1.11 9:05", datetime(2026, 11, 1, 9, 5, tzinfo=TZ)),
        # дата раньше сегодняшней без года — следующий год
        ("05.01 10:00", datetime(2027, 1, 5, 10, 0, tzinfo=TZ)),
        ("28.10.2026 18:30", datetime(2026, 10, 28, 18, 30, tzinfo=TZ)),
        ("01.01.2027  00:00", datetime(2027, 1, 1, 0, 0, tzinfo=TZ)),
        ("через 2ч", NOW + timedelta(hours=2)),
        ("+30м", NOW + timedelta(minutes=30)),
    ],
)
def test_parse_start(text, expected):
    assert parse_start(text, NOW) == expected


@pytest.mark.parametrize(
    "text",
    ["завтра", "25:00", "18:60", "31.02 10:00", "28.10 14:00", "28.10.2025 18:30", "28.10.2026", "через"],
)
def test_parse_start_rejects(text):
    with pytest.raises(ValueError):
        parse_start(text, NOW)


def test_parse_runs():
    assert parse_runs(" 5 ") == 5
    assert parse_runs("1000") == 1000
    for bad in ("0", "1001", "-1", "abc", "2.5", ""):
        with pytest.raises(ValueError):
            parse_runs(bad)


def test_format_interval():
    assert format_interval(129600) == "1 д 12 ч"
    assert format_interval(5400) == "1 ч 30 мин"
    assert format_interval(60) == "1 мин"


def test_format_ts():
    assert format_ts(int(datetime(2026, 10, 28, 18, 30, tzinfo=TZ).timestamp()), TZ) == "28.10.2026 18:30"
