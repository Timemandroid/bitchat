"""Сводка для проверки: python -m app.report

Показывает, какие события пришли от Meta и чем закончилась обработка каждого ролика.
"""
import json
import time
from collections import Counter

from .config import Settings
from .store import Store


def main() -> None:
    s = Settings.from_env()
    types: Counter = Counter()
    channels: Counter = Counter()
    total = 0
    try:
        with open(s.data_dir / "events.jsonl", encoding="utf-8") as f:
            for line in f:
                for ev in json.loads(line).get("events", []):
                    total += 1
                    channels[ev.get("channel", "?")] += 1
                    if ev.get("is_echo"):
                        types["(исходящее, эхо)"] += 1
                    elif ev.get("is_unsupported"):
                        types["(неподдерживаемое)"] += 1
                    elif not ev.get("attachment_types"):
                        types["(текст без вложений)"] += 1
                    for t in ev.get("attachment_types", []):
                        types[t] += 1
    except FileNotFoundError:
        pass
    print(f"Событий от Meta: {total}")
    print("По каналам:", dict(channels) or "нет")
    print("По типам:", dict(types) or "нет")
    print()
    rows = Store(s.data_dir / "bot.db").recent(15)
    if not rows:
        print("Обработанных вложений пока нет.")
    for r in rows:
        d = r["details"]
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created"]))
        delivery = d.get("delivery") or {}
        print(f"{when}  {r['status']:9}  тип={d.get('type')}  кадров={d.get('frames')}  "
              f"доставка={delivery.get('target')}  совпадение={delivery.get('matched_by', 'нет')}")
        if d.get("media", {}).get("error"):
            print(f"    медиа: {d['media']['error']}")
        if d.get("brief_error"):
            print(f"    бриф: {d['brief_error']}")
        if delivery.get("match_info"):
            print(f"    поиск в Битрикс24: {delivery['match_info']}")
        for k in ("match_error", "comment_error", "chat_error", "note"):
            if delivery.get(k):
                print(f"    {k}: {delivery[k]}")
        if d.get("exception"):
            print(f"    исключение: {d['exception']}")


if __name__ == "__main__":
    main()
