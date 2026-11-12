"""HTTP сервер: приём вебхуков Instagram и раздача сохранённых роликов по ссылке."""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from html import escape

from . import brief as brief_mod
from . import meta
from .bitrix import Bitrix
from .config import Settings
from .pipeline import Pipeline
from .store import Store

log = logging.getLogger("reelbot")
# httpx пишет в журнал полный адрес запроса, а в нём токен Instagram и код вебхука Битрикс24
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
NAME_RE = re.compile(r"^(video|image|frame_\d{2})\.(mp4|mov|webm|jpg|png|webp|gif)$")


def cleanup_media(settings: Settings) -> int:
    """Удаляет ролики старше MEDIA_RETENTION_DAYS, чтобы диск не переполнился."""
    if settings.media_retention_days <= 0 or not settings.media_dir.exists():
        return 0
    border = time.time() - settings.media_retention_days * 86400
    removed = 0
    for d in settings.media_dir.iterdir():
        if d.is_dir() and d.stat().st_mtime < border:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def _maintenance_loop(settings: Settings, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            meta.refresh_token_if_needed(settings)
            removed = cleanup_media(settings)
            if removed:
                log.info("удалено старых роликов: %s", removed)
        except Exception:
            log.exception("ошибка в фоновом обслуживании")
        stop.wait(6 * 3600)


def create_app(settings: Settings, background_maintenance: bool = True) -> FastAPI:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir / "bot.db")
    bitrix = Bitrix(settings)
    claude = brief_mod.build_client(settings) if settings.brief_enabled else None
    pipeline = Pipeline(settings, store, bitrix, claude)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        problems = []
        if not settings.meta_verify_token:
            problems.append("META_VERIFY_TOKEN пуст")
        if settings.verify_signature and not settings.meta_app_secrets:
            problems.append("META_APP_SECRET пуст, все вебхуки будут отклонены")
        if not settings.public_base_url:
            problems.append("PUBLIC_BASE_URL пуст, ссылки на ролики будут нерабочими")
        if not bitrix.enabled:
            problems.append("BITRIX_WEBHOOK_URL пуст, результат останется только в журнале")
        for p in problems:
            log.warning("настройка: %s", p)
        if background_maintenance:
            threading.Thread(target=_maintenance_loop, args=(settings, stop), daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="reel-bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.pipeline = pipeline

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/webhooks/instagram")
    def verify(request: Request):
        q = request.query_params
        if (
            q.get("hub.mode") == "subscribe"
            and settings.meta_verify_token
            and q.get("hub.verify_token") == settings.meta_verify_token
        ):
            log.info("Meta подтвердила адрес вебхука")
            return PlainTextResponse(q.get("hub.challenge", ""))
        log.warning("проверка вебхука не прошла: неверный verify token")
        return PlainTextResponse("forbidden", status_code=403)

    @app.post("/webhooks/instagram")
    async def receive(request: Request, background: BackgroundTasks):
        raw = await request.body()
        if settings.verify_signature and not meta.verify_signature(
            raw, request.headers.get("x-hub-signature-256"), settings.meta_app_secrets
        ):
            log.error("подпись вебхука не совпала: проверьте META_APP_SECRET")
            return PlainTextResponse("bad signature", status_code=401)
        try:
            body = json.loads(raw)
        except ValueError:
            return PlainTextResponse("bad json", status_code=400)
        items, summary = meta.extract_incoming(body)
        meta.append_event_log(settings, body, summary)
        for ev in summary:
            log.info("событие: канал=%s вложения=%s эхо=%s неподдерживаемое=%s",
                     ev["channel"], ev["attachment_types"], ev["is_echo"], ev["is_unsupported"])
        queued = 0
        for item in items:
            if item.type not in settings.process_types:
                continue
            if store.claim(item.key):
                background.add_task(pipeline.process, item)
                queued += 1
        # отвечаем сразу, обработка идёт в фоне: иначе Meta повторит доставку
        return JSONResponse({"received": True, "queued": queued})

    def _page(title: str, body: str) -> HTMLResponse:
        if not settings.company_name or not settings.contact_email:
            return HTMLResponse("Страница не настроена: заполните COMPANY_NAME и CONTACT_EMAIL", status_code=404)
        html = (
            "<!doctype html><html lang=ru><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)}</title><style>body{{font:16px/1.6 system-ui,sans-serif;"
            "max-width:720px;margin:40px auto;padding:0 16px;color:#222}}</style></head><body>"
            f"<h1>{escape(title)}</h1>{body}</body></html>"
        )
        return HTMLResponse(html)

    @app.get("/privacy")
    def privacy():
        c, m = escape(settings.company_name), escape(settings.contact_email)
        return _page("Политика конфиденциальности", f"""
<p>Этот сервис принадлежит компании {c} и помогает её менеджерам обрабатывать сообщения,
которые клиенты отправляют в Instagram Direct компании.</p>
<h2>Какие данные мы получаем</h2>
<p>Когда вы пишете нам в Instagram Direct, сервис получает от Meta ваше сообщение, вложения
(например, ролик или пост, которым вы поделились), ваш идентификатор в Instagram, имя и ник.</p>
<h2>Зачем</h2>
<p>Только чтобы менеджеры {c} увидели ваше обращение в нашей CRM и ответили вам. Для описания товара
из присланного ролика кадры из него обрабатываются сервисом искусственного интеллекта Anthropic.
Мы не продаём данные и не используем их для рекламы.</p>
<h2>Хранение</h2>
<p>Присланные ролики и кадры хранятся на нашем сервере до 30 дней и затем удаляются.
Переписка хранится в CRM компании столько, сколько нужно для обслуживания клиентов.</p>
<h2>Удаление данных</h2>
<p>Чтобы удалить ваши данные, напишите на <a href="mailto:{m}">{m}</a> или в Direct компании.
Мы удалим их в течение 30 дней. Подробнее: <a href="/data-deletion">удаление данных</a>.</p>
<h2>Контакты</h2><p>{c}, <a href="mailto:{m}">{m}</a></p>""")

    @app.get("/data-deletion")
    def data_deletion():
        c, m = escape(settings.company_name), escape(settings.contact_email)
        return _page("Удаление данных", f"""
<p>Чтобы удалить данные, которые сервис компании {c} получил из Instagram, напишите на
<a href="mailto:{m}">{m}</a> и укажите ваш ник в Instagram. Мы удалим присланные ролики,
кадры и записи о сообщениях в течение 30 дней и сообщим об этом в ответном письме.</p>""")

    @app.get("/m/{token}/{name}")
    def media(token: str, name: str):
        if not TOKEN_RE.match(token) or not NAME_RE.match(name):
            return PlainTextResponse("not found", status_code=404)
        path = settings.media_dir / token / name
        if not path.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(path)

    return app
