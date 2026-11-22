"""HTTP сервер: приём вебхуков Instagram и раздача сохранённых роликов по ссылке."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from html import escape

from . import brief as brief_mod
from . import bitrix_setup, connector, meta
from .bitrix import Bitrix, BitrixError, load_auth, oauth_refresh, save_auth
from .bitrix_setup import load_bot
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
            Bitrix(settings).keep_alive()
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
            problems.append("Битрикс24 не подключён (нет вебхука и приложение не открывали), результат останется только в журнале")
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
        # в режиме канала открытой линии в Битрикс24 уходит вся переписка, иначе только рилсы и посты
        all_types = bool(connector.active_line(settings))
        for item in items:
            if not all_types and item.type not in settings.process_types:
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

    # ------------------------------------------------------------ приложение Битрикс24

    def _sig(member_id: str, ts: str) -> str:
        msg = f"{member_id}:{ts}".encode()
        return hmac.new(settings.bitrix_client_secret.encode(), msg, hashlib.sha256).hexdigest()

    def _app_page(body: str, status: int = 200, install: bool = False) -> HTMLResponse:
        finish = "BX24.init(function(){BX24.installFinish();});" if install else ""
        html = (
            "<!doctype html><html lang=ru><head><meta charset=utf-8>"
            "<script src='https://api.bitrix24.com/api/v1/'></script>"
            "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:640px;margin:24px;color:#222}"
            "select,button{font:inherit;padding:6px 10px}pre{white-space:pre-wrap;background:#f4f6f8;"
            "padding:12px}</style></head><body>"
            f"<h2>{escape(settings.bitrix_bot_name)}</h2>{body}<script>{finish}</script></body></html>"
        )
        return HTMLResponse(html, status_code=status)

    def _verify_admin(form: dict, install: bool = False) -> tuple[dict | None, HTMLResponse | None]:
        """Страницу открыл администратор нашего портала: токены перепроверяем у Битрикс24 и сохраняем."""
        try:
            auth = oauth_refresh(settings, form.get("REFRESH_ID", ""))
        except BitrixError as e:
            log.warning("приложение Битрикс24: токены не подтверждены: %s", e)
            return None, _app_page("<p>Откройте приложение из меню Битрикс24.</p>", 403)
        saved = load_auth(settings)
        # ponytail: один портал на сервер; для Маркета хранить токены по member_id
        if saved and saved.get("member_id") != auth.get("member_id"):
            log.warning("приложение Битрикс24: попытка подключить другой портал %s", auth.get("client_endpoint"))
            return None, _app_page("<p>Этот сервер уже подключён к другому Битрикс24.</p>", 403)
        try:
            is_admin = bool(bitrix.call("user.admin", {}, auth=auth).get("result"))
        except BitrixError as e:
            return None, _app_page(f"<p>Битрикс24 не ответил: {escape(str(e))}</p>", 502)
        if not is_admin:
            return None, _app_page("<p>Настраивать бота может только администратор Битрикс24.</p>", 403, install)
        save_auth(settings, auth)
        return auth, None

    def _events_url() -> str:
        return f"{settings.public_base_url}/bitrix/events?k={_sig('events', '0')}"

    def _app_open(form: dict, install: bool) -> HTMLResponse:
        auth, err = _verify_admin(form, install)
        if err:
            return err
        lines = []
        try:
            connector.register(bitrix, settings, _events_url())
        except BitrixError as e:
            log.error("канал в Контакт-центре не зарегистрирован: %s", e)
            lines.append(f"Канал в Контакт-центре не зарегистрирован: {e}")
        return _app_status(auth["member_id"], install, lines)

    def _app_status(member_id: str, install: bool = False, log_lines: list[str] | None = None) -> HTMLResponse:
        bot = load_bot(settings)
        parts = []
        if log_lines:
            parts.append(f"<pre>{escape(chr(10).join(log_lines))}</pre>")
        line = connector.active_line(settings)
        if line:
            parts.append(f"<p>Канал «{connector.CONNECTOR_NAME}» работает в открытой линии <b>{escape(line)}</b>: "
                         "переписка, ролики и лиды видны в Контакт-центре и CRM.</p>")
        else:
            parts.append(f"<p>Чтобы переписка из Instagram попадала в CRM, откройте Контакт-центр, выберите "
                         f"«{connector.CONNECTOR_NAME}» и подключите открытую линию. Родной канал Instagram "
                         "в Контакт-центре отключите, иначе сообщения придут дважды.</p>")
        parts.append("<h3>Чат менеджеров (необязательно)</h3>")
        if bot.get("dialog_id"):
            parts.append(f"<p>Бот подключён к чату <b>{escape(bot['dialog_id'])}</b>. "
                         "Рилсы из Instagram Direct будут приходить туда.</p>")
        else:
            parts.append("<p>Бот ещё не подключён. Выберите внутренний чат менеджеров.</p>")
        try:
            chats = bitrix.recent_chats()
        except BitrixError as e:
            chats = []
            parts.append(f"<p>Список чатов не получен: {escape(str(e))}</p>")
        if chats:
            ts = str(int(time.time()))
            opts = "".join(
                f"<option value='{escape(str(c.get('id')))}'>{escape(str(c.get('title', '')))}</option>"
                for c in chats)
            parts.append(
                "<form method=post action='/bitrix/app'>"
                f"<input type=hidden name=action value=connect><input type=hidden name=ts value='{ts}'>"
                f"<input type=hidden name=sig value='{_sig(member_id, ts)}'>"
                f"<select name=dialog_id>{opts}</select> <button>Подключить к чату</button></form>"
                "<p>В списке только групповые чаты, где вы состоите. Чаты с клиентами "
                "(открытые линии) бот не примет.</p>")
        ig = "подключён" if settings.ig_access_token else "не подключён (см. README, шаг 5)"
        parts.append(f"<p>Instagram: {ig}.</p>")
        return _app_page("".join(parts), install=install)

    def _app_connect(form: dict) -> HTMLResponse:
        saved = load_auth(settings)
        ts = form.get("ts", "")
        ok = (saved and ts.isdigit() and abs(time.time() - int(ts)) < 3600
              and hmac.compare_digest(form.get("sig", ""), _sig(str(saved.get("member_id")), ts)))
        if not ok:
            return _app_page("<p>Страница устарела. Откройте приложение заново из меню Битрикс24.</p>", 403)
        lines: list[str] = []
        bitrix_setup.cmd_connect(settings, form.get("dialog_id", ""), bitrix_setup.DEFAULT_CODE, out=lines.append)
        return _app_status(str(saved["member_id"]), log_lines=lines)

    async def _bitrix_app(request: Request, install: bool):
        if not (settings.bitrix_client_id and settings.bitrix_client_secret):
            return _app_page("<p>На сервере не заданы BITRIX_CLIENT_ID и BITRIX_CLIENT_SECRET.</p>", 503)
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
        if form.get("action") == "connect":
            return await run_in_threadpool(_app_connect, form)
        return await run_in_threadpool(_app_open, form, install)

    def _connector_open(form: dict) -> HTMLResponse:
        """Администратор нажал наш канал в Контакт-центре и выбрал открытую линию."""
        if form.get("action") == "disconnect":
            saved = load_auth(settings)
            ts, line = form.get("ts", ""), form.get("line", "")
            ok = (saved and ts.isdigit() and abs(time.time() - int(ts)) < 3600 and line.isdigit()
                  and hmac.compare_digest(form.get("sig", ""), _sig(f"{saved.get('member_id')}:{line}", ts)))
            if not ok:
                return _app_page("<p>Страница устарела. Откройте канал заново в Контакт-центре.</p>", 403)
            try:
                connector.activate(bitrix, settings, line, on=False)
            except BitrixError as e:
                return _app_page(f"<p>Отключить не вышло: {escape(str(e))}</p>", 502)
            return _app_page(f"<p>Канал отключён от линии {escape(line)}.</p>")
        auth, err = _verify_admin(form)
        if err:
            return err
        try:
            opts = json.loads(form.get("PLACEMENT_OPTIONS") or "{}")
        except ValueError:
            opts = {}
        line = str(opts.get("LINE") or "")
        if not line.isdigit():
            return _app_page("<p>Битрикс24 не передал номер открытой линии. Откройте канал из Контакт-центра.</p>", 400)
        try:
            connector.activate(bitrix, settings, line, on=True)
        except BitrixError as e:
            return _app_page(f"<p>Подключить канал не вышло: {escape(str(e))}</p>", 502)
        ts = str(int(time.time()))
        sig = _sig(f"{auth['member_id']}:{line}", ts)
        ig = "" if settings.ig_access_token else (
            "<p><b>Instagram ещё не подключён на сервере</b> (README, шаг 5): сообщения пока не приходят.</p>")
        return _app_page(
            f"<p>Канал «{connector.CONNECTOR_NAME}» подключён к открытой линии <b>{escape(line)}</b>. "
            "Сообщения из Instagram Direct, ролики и бриф по рилсам появятся в чатах этой линии, "
            "лиды создаст CRM по правилам линии. Ответы менеджеров уйдут клиенту в Instagram.</p>"
            "<p>Отключите родной канал Instagram в Контакт-центре, иначе сообщения придут дважды.</p>"
            f"{ig}<form method=post action='/bitrix/connector'><input type=hidden name=action value=disconnect>"
            f"<input type=hidden name=line value='{escape(line)}'><input type=hidden name=ts value='{ts}'>"
            f"<input type=hidden name=sig value='{sig}'><button>Отключить канал от линии</button></form>")

    def _send_reply(msg: dict, line: str) -> None:
        """Ответ менеджера из открытой линии уходит клиенту в Direct."""
        mids = []
        try:
            if msg["text"]:
                mids.append(meta.send_message(settings, msg["igsid"], text=msg["text"]))
            for f in msg["files"]:
                kind = connector.attachment_type(f["name"])
                mids.append(meta.send_message(settings, msg["igsid"], attachment=(kind, f["link"])))
        except RuntimeError as e:
            # ponytail: только журнал; показать ошибку менеджеру в чате линии, если будут жалобы
            log.error("ответ в Instagram не отправлен (чат %s): %s", msg["chat_id"], e)
            return
        try:
            connector.report_delivered(bitrix, line, msg, mids)
        except BitrixError as e:
            log.warning("статус доставки в Битрикс24 не передан: %s", e)

    @app.post("/bitrix/connector")
    async def bitrix_connector(request: Request):
        if not (settings.bitrix_client_id and settings.bitrix_client_secret):
            return _app_page("<p>На сервере не заданы BITRIX_CLIENT_ID и BITRIX_CLIENT_SECRET.</p>", 503)
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
        return await run_in_threadpool(_connector_open, form)

    @app.post("/bitrix/events")
    async def bitrix_events(request: Request, background: BackgroundTasks):
        # секрет в адресе знает только Битрикс24: адрес передаётся ему при подписке на событие
        if not settings.bitrix_client_secret or not hmac.compare_digest(
                request.query_params.get("k", ""), _sig("events", "0")):
            return PlainTextResponse("forbidden", status_code=403)
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
        line = connector.active_line(settings)
        if (form.get("event", "").upper() != "ONIMCONNECTORMESSAGEADD"
                or form.get("data[CONNECTOR]") != connector.CONNECTOR_ID or not line
                or str(form.get("data[LINE]", "")) != line):
            return PlainTextResponse("ignored")
        for msg in connector.parse_outgoing(form):
            background.add_task(_send_reply, msg, line)
        return PlainTextResponse("ok")

    @app.post("/bitrix/app")
    async def bitrix_app(request: Request):
        return await _bitrix_app(request, install=False)

    @app.post("/bitrix/install")
    async def bitrix_install(request: Request):
        return await _bitrix_app(request, install=True)

    @app.get("/m/{token}/{name}")
    def media(token: str, name: str):
        if not TOKEN_RE.match(token) or not NAME_RE.match(name):
            return PlainTextResponse("not found", status_code=404)
        path = settings.media_dir / token / name
        if not path.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(path)

    return app
