"""Работа с Instagram: проверка подписи, разбор событий, скачивание медиа, профиль, токен."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Settings

log = logging.getLogger("reelbot.meta")

VIDEO_EXT = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}
IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


# ---------------------------------------------------------------- подпись


def verify_signature(raw_body: bytes, header: str | None, secrets: tuple[str, ...]) -> bool:
    """Meta подписывает тело запроса: X-Hub-Signature-256: sha256=<HMAC с секретом приложения>."""
    if not header or not header.startswith("sha256="):
        return False
    received = header.split("=", 1)[1].strip()
    for secret in secrets:
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, received):
            return True
    return False


# ---------------------------------------------------------------- разбор событий


@dataclass
class Incoming:
    """Одно вложение из входящего сообщения."""

    key: str  # уникальный ключ для защиты от дублей
    mid: str
    index: int
    type: str
    url: str
    title: str
    media_id: str
    sender_id: str
    recipient_id: str
    timestamp: int
    text: str
    channel: str  # messaging или standby

    def to_dict(self) -> dict:
        return asdict(self)


def extract_incoming(body: dict) -> tuple[list[Incoming], list[dict]]:
    """Возвращает (вложения клиентов, краткие сведения обо всех событиях для журнала)."""
    items: list[Incoming] = []
    summary: list[dict] = []
    for entry in body.get("entry", []) or []:
        for channel in ("messaging", "standby"):
            for ev in entry.get(channel, []) or []:
                msg = ev.get("message") or {}
                sender = str((ev.get("sender") or {}).get("id", ""))
                recipient = str((ev.get("recipient") or {}).get("id", ""))
                atts = msg.get("attachments") or []
                info = {
                    "channel": channel,
                    "sender": sender,
                    "mid": msg.get("mid", ""),
                    "has_text": bool(msg.get("text")),
                    "is_echo": bool(msg.get("is_echo")),
                    "is_unsupported": bool(msg.get("is_unsupported")),
                    "attachment_types": [a.get("type", "") for a in atts],
                    "other_keys": sorted(k for k in ev.keys() if k not in ("sender", "recipient", "timestamp", "message")),
                }
                summary.append(info)
                if not msg or msg.get("is_echo") or msg.get("is_deleted"):
                    continue
                mid = str(msg.get("mid", ""))
                for i, a in enumerate(atts):
                    payload = a.get("payload") or {}
                    url = payload.get("url") or ""
                    items.append(
                        Incoming(
                            key=f"{mid}:{i}",
                            mid=mid,
                            index=i,
                            type=str(a.get("type", "")),
                            url=str(url),
                            title=str(payload.get("title") or ""),
                            media_id=str(
                                payload.get("reel_video_id")
                                or payload.get("ig_post_media_id")
                                or payload.get("id")
                                or ""
                            ),
                            sender_id=sender,
                            recipient_id=recipient,
                            timestamp=int(ev.get("timestamp") or 0),
                            text=str(msg.get("text") or ""),
                            channel=channel,
                        )
                    )
    return items, summary


def append_event_log(settings: Settings, raw_body: dict, summary: list[dict]) -> None:
    """Журнал событий. По умолчанию без текстов переписки, только типы и идентификаторы."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    record: dict = {"ts": int(time.time()), "events": summary}
    if settings.log_raw_events:
        record["raw"] = raw_body
    with open(settings.data_dir / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- скачивание


@dataclass
class Media:
    path: Path | None
    kind: str  # video, image, other, error
    content_type: str
    size: int
    error: str = ""


def _host_allowed(settings: Settings, url: str) -> tuple[bool, str]:
    p = urlparse(url)
    if p.scheme != "https" and not (settings.media_allow_http and p.scheme == "http"):
        return False, f"схема {p.scheme!r} не разрешена"
    host = (p.hostname or "").lower()
    for allowed in settings.media_allowed_hosts:
        allowed = allowed.lower()
        if host == allowed or host.endswith("." + allowed):
            return True, ""
    return False, f"хост {host!r} не входит в MEDIA_ALLOWED_HOSTS"


def download_media(settings: Settings, url: str, dest_dir: Path) -> Media:
    """Скачивает вложение сразу при получении: ссылки Instagram живут недолго."""
    if not url:
        return Media(None, "error", "", 0, "в событии нет ссылки на медиа")
    ok, why = _host_allowed(settings, url)
    if not ok:
        return Media(None, "error", "", 0, why)
    dest_dir.mkdir(parents=True, exist_ok=True)
    limit = settings.max_media_mb * 1024 * 1024
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
            if r.status_code != 200:
                return Media(None, "error", "", 0, f"HTTP {r.status_code} при скачивании")
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype in VIDEO_EXT:
                kind, ext = "video", VIDEO_EXT[ctype]
            elif ctype in IMAGE_EXT:
                kind, ext = "image", IMAGE_EXT[ctype]
            elif ctype.startswith("video/"):
                kind, ext = "video", "mp4"
            elif ctype.startswith("image/"):
                kind, ext = "image", "jpg"
            else:
                return Media(None, "other", ctype, 0, f"по ссылке не видео и не картинка ({ctype or 'тип не указан'})")
            path = dest_dir / f"{kind}.{ext}"
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    size += len(chunk)
                    if size > limit:
                        f.close()
                        path.unlink(missing_ok=True)
                        return Media(None, "error", ctype, size, f"файл больше {settings.max_media_mb} МБ")
                    f.write(chunk)
        return Media(path, kind, ctype, size)
    except httpx.HTTPError as e:
        return Media(None, "error", "", 0, f"сетевая ошибка: {e.__class__.__name__}")


# ---------------------------------------------------------------- токен и профиль


def is_instagram_login_token(token: str) -> bool:
    """Токены входа через Instagram начинаются с IG, токены Страницы Facebook с EAA."""
    return token.startswith("IG")


def _token_file(settings: Settings) -> Path:
    return settings.data_dir / "ig_token.json"


def current_token(settings: Settings) -> str:
    """Обновлённый токен хранится в файле, исходный берётся из .env."""
    try:
        data = json.loads(_token_file(settings).read_text())
        if data.get("source") == settings.ig_access_token[-12:] and data.get("access_token"):
            return data["access_token"]
    except (OSError, ValueError):
        pass
    return settings.ig_access_token


def refresh_token_if_needed(settings: Settings, min_age_days: float = 7.0) -> str:
    """Долгий токен живёт 60 дней. Обновляем заранее, не чаще раза в неделю."""
    if not settings.ig_access_token:
        return "нет токена"
    if not is_instagram_login_token(settings.ig_access_token):
        # Токен Страницы или системного пользователя этим способом не обновляется
        return "обновление не требуется"
    tf = _token_file(settings)
    now = time.time()
    try:
        data = json.loads(tf.read_text())
    except (OSError, ValueError):
        data = {}
    if data.get("source") != settings.ig_access_token[-12:]:
        # новый токен в .env: запоминаем момент, обновлять можно не раньше чем через сутки
        data = {"source": settings.ig_access_token[-12:], "access_token": settings.ig_access_token, "refreshed": now}
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        tf.write_text(json.dumps(data))
        return "токен записан"
    if now - float(data.get("refreshed", 0)) < min_age_days * 86400:
        return "обновление не требуется"
    try:
        r = httpx.get(
            f"{settings.ig_graph_host}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": data["access_token"]},
            timeout=30,
        )
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("не удалось обновить токен Instagram: %s", e.__class__.__name__)
        return "ошибка сети"
    if r.status_code != 200 or not body.get("access_token"):
        log.error("Instagram отказал в обновлении токена: HTTP %s %s", r.status_code, body.get("error", {}))
        return "отказ"
    data.update(access_token=body["access_token"], refreshed=now)
    tf.write_text(json.dumps(data))
    log.info("токен Instagram обновлён, действует %s сек", body.get("expires_in"))
    return "обновлён"


def fetch_profile(settings: Settings, igsid: str) -> dict:
    """Имя и ник клиента. Запрос необязательный: при ошибке работаем без него."""
    token = current_token(settings)
    if not token or not igsid:
        return {}
    try:
        r = httpx.get(
            f"{settings.ig_graph_host}/{settings.ig_api_version}/{igsid}",
            params={"fields": "name,username", "access_token": token},
            timeout=20,
        )
        if r.status_code != 200:
            log.warning("профиль клиента не получен: HTTP %s", r.status_code)
            return {}
        data = r.json()
        return {"name": str(data.get("name") or ""), "username": str(data.get("username") or "")}
    except (httpx.HTTPError, ValueError) as e:
        log.warning("профиль клиента не получен: %s", e.__class__.__name__)
        return {}
