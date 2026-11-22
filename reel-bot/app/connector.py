"""Свой канал Instagram в Контакт-центре Битрикс24 (REST imconnector), как у Wazzup.

Входящие из Instagram уходят в открытую линию: текст, ролик файлом и бриф. Открытая линия
сама создаёт лид или находит клиента в CRM. Ответ менеджера приходит событием
OnImConnectorMessageAdd, бот отправляет его клиенту в Instagram.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

from .bitrix import Bitrix, BitrixError
from .config import Settings

log = logging.getLogger("reelbot.connector")

CONNECTOR_ID = "reelbot_instagram"
CONNECTOR_NAME = "Instagram с рилсами"
ICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><rect x='3' y='3' width='18' "
    "height='18' rx='5' fill='none' stroke='#fff' stroke-width='2'/><circle cx='12' cy='12' r='4' "
    "fill='none' stroke='#fff' stroke-width='2'/><circle cx='17.5' cy='6.5' r='1.3' fill='#fff'/></svg>"
)


def _file(settings: Settings):
    return settings.data_dir / "bitrix_connector.json"


def load(settings: Settings) -> dict:
    try:
        data = json.loads(_file(settings).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(settings: Settings, data: dict) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _file(settings).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def active_line(settings: Settings) -> str:
    return str(load(settings).get("line") or "")


# ------------------------------------------------------------ установка


def register(b: Bitrix, settings: Settings, events_url: str) -> None:
    """Канал в списке Контакт-центра и подписка на ответы менеджеров. Повторный вызов безопасен."""
    b.call("imconnector.register", {
        "ID": CONNECTOR_ID,
        "NAME": CONNECTOR_NAME,
        "ICON": {
            "DATA_IMAGE": "data:image/svg+xml;charset=US-ASCII," + quote(ICON_SVG),
            "COLOR": "#D6249F",
            "SIZE": "90%",
            "POSITION": "center",
        },
        "PLACEMENT_HANDLER": f"{settings.public_base_url}/bitrix/connector",
    })
    for event in ("OnImConnectorMessageAdd",):
        try:
            b.call("event.bind", {"event": event, "handler": events_url})
        except BitrixError as e:
            if "already" not in str(e).lower():  # повторная подписка: Битрикс24 отвечает ошибкой, это норма
                raise
    save(settings, {**load(settings), "registered": True})


def activate(b: Bitrix, settings: Settings, line: str, on: bool) -> None:
    b.call("imconnector.activate", {"CONNECTOR": CONNECTOR_ID, "LINE": int(line), "ACTIVE": 1 if on else 0})
    if on:
        b.call("imconnector.connector.data.set", {
            "CONNECTOR": CONNECTOR_ID, "LINE": int(line),
            "DATA": {"id": f"{CONNECTOR_ID}_{line}", "url_im": "https://www.instagram.com/direct/inbox/",
                     "name": CONNECTOR_NAME},
        })
    data = load(settings)
    data["line"] = line if on else ""
    save(settings, data)


# ------------------------------------------------------------ входящие


def send_incoming(b: Bitrix, settings: Settings, line: str, *, msg_id: str, igsid: str, profile: dict,
                  text: str, file_url: str = "", file_name: str = "", timestamp_ms: int = 0) -> dict:
    name = profile.get("name") or profile.get("username") or f"Instagram {igsid}"
    user: dict = {"id": igsid, "name": name, "skip_phone_validate": "Y"}
    if profile.get("username"):
        user["url"] = f"https://www.instagram.com/{profile['username']}/"
    message: dict = {"id": msg_id, "text": text}
    if timestamp_ms:
        message["date"] = int(timestamp_ms // 1000)
    if file_url:
        message["files"] = [{"url": file_url, "name": file_name or file_url.rsplit("/", 1)[-1]}]
    body = b.call("imconnector.send.messages", {
        "CONNECTOR": CONNECTOR_ID, "LINE": int(line),
        "MESSAGES": [{"user": user, "message": message, "chat": {"id": igsid, "name": name}}],
    })
    result = body.get("result") or {}
    data = (result.get("DATA") or {}).get("RESULT") or [{}]
    first = data[0] if isinstance(data, list) and data else {}
    return {"target": "open_line", "line": line, "session": (first.get("session") or {}).get("ID"),
            "ok": bool(result.get("SUCCESS", True))}


# ------------------------------------------------------------ ответы менеджера


_BB_BR = re.compile(r"\[br\]", re.I)
_BB_TAG = re.compile(r"\[/?(?:b|i|u|s|url|color|size|quote|code|user|chat|icon)(?:=[^\]]*)?\]", re.I)


def plain_text(bb: str) -> str:
    """Битрикс24 присылает текст с BB-кодами, Instagram их не понимает."""
    return _BB_TAG.sub("", _BB_BR.sub("\n", bb or "")).strip()


def attachment_type(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("jpg", "jpeg", "png", "gif", "webp"):
        return "image"
    if ext in ("mp4", "mov", "webm"):
        return "video"
    if ext in ("mp3", "m4a", "aac", "wav", "ogg"):
        return "audio"
    return "file"


def parse_outgoing(form: dict) -> list[dict]:
    """Разбирает событие ONIMCONNECTORMESSAGEADD (форма с ключами вида data[MESSAGES][0][...])."""
    out = []
    i = 0
    while f"data[MESSAGES][{i}][chat][id]" in form:
        p = f"data[MESSAGES][{i}]"
        files = []
        j = 0
        while f"{p}[message][files][{j}][link]" in form:
            files.append({"link": form[f"{p}[message][files][{j}][link]"],
                          "name": form.get(f"{p}[message][files][{j}][name]", "")})
            j += 1
        out.append({
            "igsid": form[f"{p}[chat][id]"],
            "chat_id": form.get(f"{p}[im][chat_id]", ""),
            "message_id": form.get(f"{p}[im][message_id]", ""),
            "text": plain_text(form.get(f"{p}[message][text]", "")),
            "files": files,
        })
        i += 1
    return out


def report_delivered(b: Bitrix, line: str, msg: dict, ig_mids: list[str]) -> None:
    b.call("imconnector.send.status.delivery", {
        "CONNECTOR": CONNECTOR_ID, "LINE": int(line),
        "MESSAGES": [{"im": {"chat_id": msg["chat_id"], "message_id": msg["message_id"]},
                      "message": {"id": ig_mids}, "chat": {"id": msg["igsid"]}}],
    })
