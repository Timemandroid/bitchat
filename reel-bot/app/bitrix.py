"""Битрикс24 через приложение (OAuth) или входящий вебхук: поиск клиента, комментарий в ленту, сообщение в чат."""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Settings

log = logging.getLogger("reelbot.bitrix")

SELECT = {
    "lead": ["ID", "TITLE", "NAME", "LAST_NAME", "IM", "DATE_MODIFY"],
    "contact": ["ID", "NAME", "LAST_NAME", "IM", "DATE_MODIFY"],
}


class BitrixError(Exception):
    pass


@dataclass
class Match:
    entity_type: str
    entity_id: int
    how: str  # igsid или name
    url: str


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


_refresh_lock = threading.Lock()


def load_auth(settings: Settings) -> dict:
    """Токены приложения Битрикс24, сохранённые при открытии приложения администратором."""
    try:
        data = json.loads(settings.app_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("access_token") else {}
    except (OSError, ValueError):
        return {}


def save_auth(settings: Settings, auth: dict) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    auth = {**auth, "saved_at": time.time()}
    settings.app_file.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        settings.app_file.chmod(0o600)
    except OSError:
        pass


def oauth_refresh(settings: Settings, refresh_token: str) -> dict:
    """Обменивает refresh token на новые токены через сервер авторизации Битрикс24.

    Ответ пришёл от oauth.bitrix.info и подписан нашим client_secret, поэтому адресу портала
    (client_endpoint) и member_id из него можно верить, в отличие от полей формы.
    """
    try:
        r = httpx.get(settings.bitrix_oauth_url, timeout=30, params={
            "grant_type": "refresh_token",
            "client_id": settings.bitrix_client_id,
            "client_secret": settings.bitrix_client_secret,
            "refresh_token": refresh_token,
        })
        body = r.json()
    except httpx.HTTPError as e:
        raise BitrixError(f"oauth: сетевая ошибка {e.__class__.__name__}") from e
    except ValueError as e:
        raise BitrixError(f"oauth: ответ не JSON (HTTP {r.status_code})") from e
    if "error" in body or not body.get("access_token") or not body.get("client_endpoint"):
        raise BitrixError(f"oauth: {body.get('error', 'нет токена')} {body.get('error_description', '')}".strip())
    return {k: body.get(k) for k in
            ("access_token", "refresh_token", "client_endpoint", "member_id", "user_id", "expires_in")}


class Bitrix:
    """Ходит в Битрикс24 токеном приложения, если приложение установлено, иначе через вебхук."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.webhook = settings.bitrix_webhook_url.rstrip("/")

    def _target(self) -> tuple[str, dict]:
        auth = load_auth(self.s)
        if auth:
            return auth["client_endpoint"].rstrip("/"), auth
        return self.webhook, {}

    @property
    def enabled(self) -> bool:
        return bool(self._target()[0])

    @property
    def portal(self) -> str:
        host = urlparse(self._target()[0]).netloc
        return f"https://{host}" if host else ""

    def refresh(self, used_token: str) -> dict:
        with _refresh_lock:
            auth = load_auth(self.s)
            if auth.get("refresh_token") != used_token:
                return auth  # другой поток уже обновил
            new = {**auth, **oauth_refresh(self.s, used_token)}
            save_auth(self.s, new)
            return new

    def keep_alive(self, max_age_sec: float = 86400) -> None:
        """refresh token живёт 28 дней: обновляем заранее, даже если рилсов не было."""
        auth = load_auth(self.s)
        if auth and time.time() - float(auth.get("saved_at") or 0) > max_age_sec:
            self.refresh(auth["refresh_token"])

    def _post(self, url: str, method: str, params: dict, timeout: float) -> dict:
        try:
            r = httpx.post(url, json=params, timeout=timeout)
            return r.json()
        except httpx.HTTPError as e:
            raise BitrixError(f"{method}: сетевая ошибка {e.__class__.__name__}") from e
        except ValueError as e:
            raise BitrixError(f"{method}: ответ не JSON (HTTP {r.status_code})") from e

    def call(self, method: str, params: dict, timeout: float = 60, auth: dict | None = None) -> dict:
        """auth: чужие токены (например, только что открывшего приложение), без сохранения."""
        if auth:
            base = auth["client_endpoint"].rstrip("/")
        else:
            base, auth = self._target()
        url = f"{base}/{method}.json"
        if auth:
            body = self._post(url, method, {**params, "auth": auth["access_token"]}, timeout)
            if body.get("error") == "expired_token" and auth.get("saved_at"):
                auth = self.refresh(auth["refresh_token"])
                body = self._post(url, method, {**params, "auth": auth["access_token"]}, timeout)
        else:
            body = self._post(url, method, params, timeout)
        if "error" in body:
            raise BitrixError(f"{method}: {body.get('error')} {body.get('error_description', '')}".strip())
        return body

    # ------------------------------------------------------------ поиск клиента

    def find_entity(self, igsid: str, names: list[str]) -> tuple[Match | None, dict]:
        """Ищет лид или контакт клиента среди недавно изменённых.

        Сильное совпадение: ID клиента из Instagram есть в поле мессенджеров (IM).
        Слабое совпадение: совпало имя или ник. Берём результат, только если он один.
        """
        since = datetime.now(timezone.utc) - timedelta(minutes=self.s.bitrix_match_window_min)
        wanted = [n for n in (_norm(x) for x in names) if len(n) >= 3]
        strong: list[Match] = []
        weak: list[Match] = []
        seen = 0
        for etype in self.s.bitrix_entity_types:
            if etype not in SELECT:
                continue
            body = self.call(
                f"crm.{etype}.list",
                {
                    "filter": {">DATE_MODIFY": since.isoformat(timespec="seconds")},
                    "select": SELECT[etype],
                    "order": {"DATE_MODIFY": "DESC"},
                },
            )
            for row in body.get("result") or []:
                seen += 1
                eid = int(row["ID"])
                url = f"{self.portal}/crm/{etype}/details/{eid}/"
                im_values = [str(x.get("VALUE", "")) for x in (row.get("IM") or []) if isinstance(x, dict)]
                if igsid and any(igsid in v for v in im_values):
                    strong.append(Match(etype, eid, "igsid", url))
                    continue
                full = _norm(f"{row.get('NAME') or ''} {row.get('LAST_NAME') or ''}")
                title = _norm(row.get("TITLE") or "")
                if any(w == full or (title and w in title) for w in wanted):
                    weak.append(Match(etype, eid, "name", url))
        info = {"checked": seen, "by_igsid": len(strong), "by_name": len(weak)}
        if len(strong) == 1:
            return strong[0], info
        if not strong and len(weak) == 1:
            return weak[0], info
        return None, info

    # ------------------------------------------------------------ запись

    def add_comment(self, match: Match, text: str, files: list[Path]) -> int:
        fields: dict = {"ENTITY_ID": match.entity_id, "ENTITY_TYPE": match.entity_type, "COMMENT": text}
        if files:
            fields["FILES"] = [
                [p.name, base64.standard_b64encode(p.read_bytes()).decode("ascii")] for p in files
            ]
        body = self.call("crm.timeline.comment.add", {"fields": fields}, timeout=300)
        return int(body.get("result") or 0)

    def send_message(self, dialog_id: str, text: str, attach: dict | None = None) -> int:
        """Сообщение от имени сотрудника, который создал вебхук."""
        params: dict = {"DIALOG_ID": dialog_id, "MESSAGE": text, "URL_PREVIEW": "N"}
        if attach:
            params["ATTACH"] = attach
        body = self.call("im.message.add", params)
        return int(body.get("result") or 0)

    # ------------------------------------------------------------ чат-бот

    def bot_register(self, code: str, token: str, name: str) -> int:
        """Регистрирует бота. Повторный вызов с тем же code возвращает того же бота без изменений."""
        fields = {
            "code": code,
            "botToken": token,
            "properties": {"name": name, "workPosition": "Разбор рилсов из Instagram", "color": "AQUA"},
            "type": "bot",
            # бот только пишет в чат и ничего не читает, поэтому события ему не нужны
            "eventMode": "fetch",
        }
        body = self.call("imbot.v2.Bot.register", {"fields": fields})
        return int(((body.get("result") or {}).get("bot") or {}).get("id") or 0)

    def bot_send(self, bot_id: int, token: str, dialog_id: str, text: str, attach: dict | None = None) -> int:
        fields: dict = {"message": text, "urlPreview": False}
        if attach:
            fields["attach"] = attach
        body = self.call(
            "imbot.v2.Chat.Message.send",
            {"botId": bot_id, "botToken": token, "dialogId": dialog_id, "fields": fields},
        )
        return int((body.get("result") or {}).get("id") or 0)

    def chat_add_users(self, chat_id: int, user_ids: list[int]) -> bool:
        body = self.call("im.chat.user.add", {"CHAT_ID": chat_id, "USERS": user_ids})
        return bool(body.get("result"))

    def dialog_info(self, dialog_id: str) -> dict:
        body = self.call("im.dialog.get", {"DIALOG_ID": dialog_id})
        return body.get("result") or {}

    def recent_chats(self) -> list[dict]:
        body = self.call("im.recent.list", {"SKIP_OPENLINES": "Y", "SKIP_DIALOG": "Y", "LIMIT": 50})
        result = body.get("result") or {}
        items = result.get("items", []) if isinstance(result, dict) else result
        return [i for i in items if isinstance(i, dict) and str(i.get("id", "")).startswith("chat")]


def is_open_line(info: dict) -> bool:
    """Чат открытой линии: в нём сотрудники переписываются с клиентом через внешний канал."""
    values = [str(info.get(k) or "").lower() for k in ("type", "entity_type", "message_type")]
    return any(v in ("lines", "livechat") for v in values)


def build_attach(video_url: str, frame_urls: list[str]) -> dict | None:
    """Карточка под сообщением: кадры картинками и ссылка на ролик."""
    blocks: list[dict] = []
    if frame_urls:
        blocks.append({"IMAGE": [{"NAME": f"Кадр {i}", "LINK": u} for i, u in enumerate(frame_urls, start=1)]})
    if video_url:
        blocks.append({"LINK": {"NAME": "Смотреть ролик", "LINK": video_url}})
    if not blocks:
        return None
    return {"ID": 1, "COLOR_TOKEN": "primary", "BLOCKS": blocks}
