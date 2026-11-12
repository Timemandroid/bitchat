"""Тестовое окружение: поддельные серверы Meta, Claude и Битрикс24 на локальном порту."""
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.config import Settings

for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(var, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

BRIEF = {
    "is_product_video": True,
    "product": "женская куртка бомбер из чёрной экокожи",
    "category": "верхняя одежда",
    "brand_or_model": "",
    "attributes": ["чёрный цвет", "оверсайз"],
    "on_screen_text": "новая коллекция",
    "price_mentioned": "12 900",
    "catalog_match": "",
    "client_intent": "узнать наличие и размеры",
    "questions_to_ask": ["Какой размер нужен?"],
    "confidence": "medium",
}


class Fake:
    """Состояние поддельных сервисов, которое тесты могут менять и читать."""

    def __init__(self, video: Path):
        self.video = video
        self.calls: list[dict] = []
        self.leads: list[dict] = []
        self.contacts: list[dict] = []
        self.claude_status = 200
        self.comment_fail_with_video = False
        self.port = 0
        # чат и бот в поддельном Битрикс24
        self.dialog = {"name": "Менеджеры", "type": "chat", "entity_type": ""}
        self.recent = []
        self.bot_in_chat = False
        self.chat_add_fails = False
        self.attach_fail = False

    def by_path(self, part: str) -> list[dict]:
        return [c for c in self.calls if part in c["path"]]


def _handler(fake: Fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            fake.calls.append({"method": "GET", "path": self.path})
            if self.path.startswith("/cdn/reel.mp4"):
                return self._send(200, fake.video.read_bytes(), "video/mp4")
            if self.path.startswith("/cdn/page"):
                return self._send(200, b"<html>reel page</html>", "text/html; charset=utf-8")
            if self.path.startswith("/v27.0/me/subscribed_apps"):
                return self._send(200, {"data": [{"subscribed_fields": ["messages"]}]})
            if self.path.startswith("/v27.0/me"):
                return self._send(200, {"user_id": "17841400000000000", "username": "shop"})
            if self.path.startswith("/v27.0/"):
                return self._send(200, {"name": "Анна К", "username": "anna_k", "id": "111"})
            return self._send(404, {"error": "nope"})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw) if raw else {}
            fake.calls.append({"method": "POST", "path": self.path, "body": body,
                               "headers": {k.lower(): v for k, v in self.headers.items()}})
            if self.path.startswith("/v27.0/me/subscribed_apps"):
                return self._send(200, {"success": True})
            if self.path.startswith("/v1/messages"):
                if fake.claude_status != 200:
                    return self._send(fake.claude_status, {"type": "error", "error": {
                        "type": "authentication_error", "message": "invalid x-api-key"}})
                return self._send(200, {
                    "id": "msg_test", "type": "message", "role": "assistant",
                    "model": body.get("model", ""),
                    "content": [{"type": "text", "text": json.dumps(BRIEF, ensure_ascii=False)}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 6000, "output_tokens": 300},
                })
            if self.path.endswith("crm.lead.list.json"):
                return self._send(200, {"result": fake.leads, "total": len(fake.leads)})
            if self.path.endswith("crm.contact.list.json"):
                return self._send(200, {"result": fake.contacts, "total": len(fake.contacts)})
            if self.path.endswith("crm.timeline.comment.add.json"):
                names = [f[0] for f in body.get("fields", {}).get("FILES", [])]
                if fake.comment_fail_with_video and any(n.startswith("video.") for n in names):
                    return self._send(400, {"error": "ERROR_FILE", "error_description": "too large"})
                return self._send(200, {"result": 77})
            if self.path.endswith("im.message.add.json"):
                if fake.attach_fail and body.get("ATTACH"):
                    return self._send(400, {"error": "ATTACH_ERROR", "error_description": "bad attach"})
                return self._send(200, {"result": 5})
            if self.path.endswith("im.dialog.get.json"):
                return self._send(200, {"result": fake.dialog})
            if self.path.endswith("im.recent.list.json"):
                return self._send(200, {"result": {"items": fake.recent, "hasMore": False}})
            if self.path.endswith("imbot.v2.Bot.register.json"):
                return self._send(200, {"result": {"bot": {"id": 501}, "users": []}})
            if self.path.endswith("im.chat.user.add.json"):
                if fake.chat_add_fails:
                    return self._send(400, {"error": "ACCESS_ERROR", "error_description": "no rights"})
                fake.bot_in_chat = True
                return self._send(200, {"result": True})
            if self.path.endswith("imbot.v2.Chat.Message.send.json"):
                if not fake.bot_in_chat:
                    return self._send(400, {"error": "ACCESS_DENIED",
                                            "error_description": "Бот не является участником чата"})
                if fake.attach_fail and body.get("fields", {}).get("attach"):
                    return self._send(400, {"error": "ATTACH_ERROR", "error_description": "bad attach"})
                return self._send(200, {"result": {"id": 900, "uuidMap": {}, "failureMap": {}}})
            return self._send(404, {"error": "ERROR_METHOD_NOT_FOUND"})

    return H


@pytest.fixture(scope="session")
def video(tmp_path_factory) -> Path:
    """Вертикальный тестовый ролик 6 секунд, как у рилса."""
    path = tmp_path_factory.mktemp("video") / "reel.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=6:size=720x1280:rate=15", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path


@pytest.fixture()
def fake(video):
    f = Fake(video)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(f))
    f.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f
    server.shutdown()


@pytest.fixture()
def settings(tmp_path, fake) -> Settings:
    base = f"http://127.0.0.1:{fake.port}"
    return Settings(
        data_dir=tmp_path / "data",
        public_base_url="https://reels.example.kz",
        meta_verify_token="verify-me",
        meta_app_secrets=("app-secret",),
        ig_access_token="IGAA-test-token-000000000000",
        ig_graph_host=base,
        media_allowed_hosts=("127.0.0.1",),
        media_allow_http=True,
        claude_base_url=base,
        anthropic_api_key="test-key",
        bitrix_webhook_url=f"{base}/rest/1/secret",
        bitrix_dialog_id="chat42",
        bitrix_match_retries=1,
        bitrix_match_retry_sec=0,
    )
