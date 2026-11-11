"""Включает доставку сообщений для аккаунта Instagram: python -m app.subscribe

Один раз после настройки вебхука в кабинете Meta. Команда печатает ответы Instagram как есть,
чтобы ошибку было видно сразу.
"""
import sys

import httpx

from .config import Settings
from . import meta


def run(settings: Settings, fields: str = "messages") -> bool:
    token = meta.current_token(settings)
    if not token:
        print("В .env не задан IG_ACCESS_TOKEN.")
        return False
    base = f"{settings.ig_graph_host}/{settings.ig_api_version}"
    ok = True
    steps = [
        ("Аккаунт", "GET", f"{base}/me", {"fields": "user_id,username"}),
        ("Включение подписки", "POST", f"{base}/me/subscribed_apps", {"subscribed_fields": fields}),
        ("Текущие подписки", "GET", f"{base}/me/subscribed_apps", {}),
    ]
    for title, method, url, params in steps:
        try:
            r = httpx.request(method, url, params={**params, "access_token": token}, timeout=30)
            print(f"{title}: HTTP {r.status_code} {r.text[:500]}")
            ok = ok and r.status_code == 200
        except httpx.HTTPError as e:
            print(f"{title}: сетевая ошибка {e.__class__.__name__}")
            ok = False
    return ok


if __name__ == "__main__":
    fields = sys.argv[1] if len(sys.argv) > 1 else "messages"
    sys.exit(0 if run(Settings.from_env(), fields) else 1)
