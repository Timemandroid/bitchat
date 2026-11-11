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
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

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

    @app.get("/m/{token}/{name}")
    def media(token: str, name: str):
        if not TOKEN_RE.match(token) or not NAME_RE.match(name):
            return PlainTextResponse("not found", status_code=404)
        path = settings.media_dir / token / name
        if not path.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(path)

    return app
