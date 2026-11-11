"""Имитация события от Instagram, чтобы проверить бота без приложения Meta.

Пример:
    python -m app.simulate --url https://example.com/video.mp4 --text "есть в наличии?"

Хост из ссылки должен быть в MEDIA_ALLOWED_HOSTS.
"""
import argparse
import hashlib
import hmac
import json
import os
import time

import httpx


def main() -> None:
    p = argparse.ArgumentParser(description="Отправить боту тестовое событие с роликом")
    p.add_argument("--url", required=True, help="прямая ссылка на видеофайл или картинку")
    p.add_argument("--text", default="", help="текст, который клиент написал вместе с роликом")
    p.add_argument("--title", default="", help="подпись автора ролика")
    p.add_argument("--sender", default="1000000000000001", help="ID клиента в Instagram")
    p.add_argument("--type", default="ig_reel")
    p.add_argument("--server", default="http://127.0.0.1:8000")
    a = p.parse_args()

    secret = os.environ.get("META_APP_SECRET", "").split(",")[0].strip()
    msg = {"mid": f"sim-{int(time.time() * 1000)}",
           "attachments": [{"type": a.type, "payload": {"url": a.url, "title": a.title}}]}
    if a.text:
        msg["text"] = a.text
    body = {"object": "instagram", "entry": [{"id": "0", "time": int(time.time()), "messaging": [
        {"sender": {"id": a.sender}, "recipient": {"id": "0"},
         "timestamp": int(time.time() * 1000), "message": msg}]}]}
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    r = httpx.post(f"{a.server}/webhooks/instagram", content=raw, trust_env=False,
                   headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"}, timeout=30)
    print(r.status_code, r.text)
    print("Результат смотрите командой: python -m app.report")


if __name__ == "__main__":
    main()
