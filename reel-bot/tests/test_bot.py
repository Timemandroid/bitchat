import hashlib
import hmac
import json
import subprocess

import pytest
from dataclasses import replace

from fastapi.testclient import TestClient

from app import frames, meta
from app.main import create_app


def sign(raw: bytes, secret: str = "app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def event(url: str, mid: str = "mid-1", type_: str = "ig_reel", channel: str = "messaging", text: str = "") -> dict:
    msg = {"mid": mid, "attachments": [{"type": type_, "payload": {
        "url": url, "title": "Новая коллекция, размеры от S до XL", "reel_video_id": "987"}}]}
    if text:
        msg["text"] = text
    return {"object": "instagram", "entry": [{"id": "17841400000000000", "time": 1,
            channel: [{"sender": {"id": "111"}, "recipient": {"id": "17841400000000000"},
                       "timestamp": 1731300000000, "message": msg}]}]}


def post(client: TestClient, body: dict, secret: str = "app-secret"):
    raw = json.dumps(body).encode()
    return client.post("/webhooks/instagram", content=raw,
                       headers={"X-Hub-Signature-256": sign(raw, secret), "Content-Type": "application/json"})


def make_client(settings) -> TestClient:
    return TestClient(create_app(settings, background_maintenance=False))


# ------------------------------------------------------------------ мелкие части


def test_signature_accepts_any_configured_secret():
    raw = b'{"a":1}'
    assert meta.verify_signature(raw, sign(raw, "s2"), ("s1", "s2"))
    assert not meta.verify_signature(raw, sign(raw, "other"), ("s1", "s2"))
    assert not meta.verify_signature(raw, None, ("s1",))
    assert not meta.verify_signature(raw, "sha1=abc", ("s1",))


def test_extract_reads_messaging_and_standby_and_skips_echo():
    body = event("https://x/1.mp4", channel="standby")
    body["entry"][0]["messaging"] = [
        {"sender": {"id": "17841400000000000"}, "recipient": {"id": "111"}, "timestamp": 1,
         "message": {"mid": "echo", "is_echo": True, "attachments": [{"type": "ig_reel", "payload": {"url": "u"}}]}},
        {"sender": {"id": "222"}, "recipient": {"id": "17841400000000000"}, "timestamp": 2,
         "message": {"mid": "plain", "text": "привет"}},
    ]
    items, summary = meta.extract_incoming(body)
    assert [(i.mid, i.channel, i.type) for i in items] == [("mid-1", "standby", "ig_reel")]
    assert items[0].title.startswith("Новая коллекция") and items[0].media_id == "987"
    assert len(summary) == 3
    # в кратком журнале нет текста переписки
    assert "привет" not in json.dumps(summary, ensure_ascii=False)


def test_frames_are_evenly_spaced_and_downscaled(video, tmp_path):
    got, duration = frames.extract_frames(video, tmp_path, count=4, max_side=960)
    assert 5.5 < duration < 6.5
    assert [f.second for f in got] == [0.8, 2.2, 3.8, 5.2]
    dims = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(got[0].path)], capture_output=True, text=True).stdout.strip()
    assert dims == "540,960"


# ------------------------------------------------------------------ вебхук


def test_verify_handshake(settings):
    c = make_client(settings)
    ok = c.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                              "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    bad = c.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                               "hub.challenge": "1"})
    assert bad.status_code == 403


def test_bad_signature_is_rejected(settings, fake):
    c = make_client(settings)
    r = post(c, event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4"), secret="wrong")
    assert r.status_code == 401
    assert not fake.calls


def test_plain_video_type_is_ignored(settings, fake):
    c = make_client(settings)
    r = post(c, event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4", type_="video"))
    assert r.json() == {"received": True, "queued": 0}
    assert not fake.calls


# ------------------------------------------------------------------ полный путь


def test_reel_goes_to_matched_lead_with_brief_and_video(settings, fake):
    fake.leads = [
        {"ID": "10", "TITLE": "Кто-то другой", "NAME": "Пётр", "LAST_NAME": "", "IM": []},
        {"ID": "15", "TITLE": "Instagram Direct", "NAME": "Anna", "LAST_NAME": "",
         "IM": [{"ID": "1", "VALUE_TYPE": "IMOL", "VALUE": "imol|fbinstagramdirect|2|111|35", "TYPE_ID": "IM"}]},
    ]
    c = make_client(settings)
    url = f"http://127.0.0.1:{fake.port}/cdn/reel.mp4"
    r = post(c, event(url, text="есть такая в наличии?"))
    assert r.status_code == 200 and r.json()["queued"] == 1

    # запрос к Claude: 8 кадров, структурный ответ, запасная модель включена
    claude = fake.by_path("/v1/messages")
    assert len(claude) == 1
    req = claude[0]["body"]
    assert req["model"] == "claude-opus-5-5"
    images = [b for b in req["messages"][0]["content"] if b["type"] == "image"]
    assert len(images) == 8 and images[0]["source"]["media_type"] == "image/jpeg"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["effort"] == "medium"
    assert req["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in claude[0]["headers"]["anthropic-beta"]
    tail = req["messages"][0]["content"][-1]["text"]
    assert "Новая коллекция" in tail and "есть такая в наличии?" in tail

    # комментарий в ленте нужного лида: текст, три кадра и сам ролик
    comments = fake.by_path("crm.timeline.comment.add")
    assert len(comments) == 1
    fields = comments[0]["body"]["fields"]
    assert (fields["ENTITY_TYPE"], fields["ENTITY_ID"]) == ("lead", 15)
    assert [f[0] for f in fields["FILES"]] == ["frame_01.jpg", "frame_02.jpg", "frame_03.jpg", "video.mp4"]
    text = fields["COMMENT"]
    assert "Клиент прислал рилс в Instagram" in text
    assert "От: Анна К (@anna_k)" in text
    assert "Товар: женская куртка бомбер из чёрной экокожи" in text
    assert "уверенность: средняя" in text
    assert "Сообщение клиента: есть такая в наличии?" in text

    # ссылка из комментария действительно отдаёт видео, в том числе по частям
    link = next(line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("Смотреть ролик"))
    assert link.startswith("https://reels.example.kz/m/")
    path = link.replace("https://reels.example.kz", "")
    full = c.get(path)
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
    assert full.content == fake.video.read_bytes()
    part = c.get(path, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100

    # в общий чат уходит короткое уведомление со ссылкой на карточку
    chat = fake.by_path("im.message.add")
    assert len(chat) == 1 and chat[0]["body"]["DIALOG_ID"] == "chat42"
    assert "/crm/lead/details/15/" in chat[0]["body"]["MESSAGE"]

    saved = c.app.state.store.get("mid-1:0")
    assert saved["status"] == "done"
    assert saved["details"]["delivery"]["matched_by"] == "igsid"
    assert saved["details"]["delivery"]["video_attached"] is True


def test_match_by_name_when_id_is_absent(settings, fake):
    fake.contacts = [{"ID": "7", "NAME": "Анна", "LAST_NAME": "К", "IM": []}]
    c = make_client(settings)
    post(c, event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4"))
    fields = fake.by_path("crm.timeline.comment.add")[0]["body"]["fields"]
    assert (fields["ENTITY_TYPE"], fields["ENTITY_ID"]) == ("contact", 7)
    assert c.app.state.store.get("mid-1:0")["details"]["delivery"]["matched_by"] == "name"


def test_ambiguous_name_is_not_guessed_and_goes_to_chat(settings, fake):
    fake.leads = [{"ID": "1", "TITLE": "", "NAME": "Анна", "LAST_NAME": "К", "IM": []},
                  {"ID": "2", "TITLE": "", "NAME": "Анна", "LAST_NAME": "К", "IM": []}]
    c = make_client(settings)
    post(c, event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4"))
    assert not fake.by_path("crm.timeline.comment.add")
    msg = fake.by_path("im.message.add")[0]["body"]["MESSAGE"]
    assert msg.startswith("Карточка клиента не найдена автоматически.")
    assert "Смотреть ролик" in msg and "Товар:" in msg
    assert c.app.state.store.get("mid-1:0")["details"]["delivery"]["target"] == "chat"


def test_duplicate_delivery_is_processed_once(settings, fake):
    c = make_client(settings)
    body = event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4")
    assert post(c, body).json()["queued"] == 1
    assert post(c, body).json()["queued"] == 0
    assert len(fake.by_path("/v1/messages")) == 1


def test_video_too_big_for_bitrix_is_sent_as_link_only(settings, fake):
    fake.leads = [{"ID": "15", "TITLE": "", "NAME": "", "LAST_NAME": "", "IM": [{"VALUE": "imol|x|111|1"}]}]
    c = make_client(replace(settings, bitrix_attach_video_max_mb=0))
    post(c, event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4"))
    files = fake.by_path("crm.timeline.comment.add")[0]["body"]["fields"]["FILES"]
    assert [f[0] for f in files] == ["frame_01.jpg", "frame_02.jpg", "frame_03.jpg"]


def test_comment_is_retried_without_video_when_bitrix_rejects_it(settings, fake):
    fake.leads = [{"ID": "15", "TITLE": "", "NAME": "", "LAST_NAME": "", "IM": [{"VALUE": "imol|x|111|1"}]}]
    fake.comment_fail_with_video = True
    c = make_client(settings)
    post(c, event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4"))
    assert len(fake.by_path("crm.timeline.comment.add")) == 2
    d = c.app.state.store.get("mid-1:0")["details"]["delivery"]
    assert d["target"] == "timeline" and d["video_attached"] is False


def test_claude_failure_still_delivers_the_video(settings, fake):
    fake.claude_status = 401
    fake.leads = [{"ID": "15", "TITLE": "", "NAME": "", "LAST_NAME": "", "IM": [{"VALUE": "imol|x|111|1"}]}]
    c = make_client(settings)
    post(c, event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4"))
    text = fake.by_path("crm.timeline.comment.add")[0]["body"]["fields"]["COMMENT"]
    assert "Смотреть ролик" in text
    assert "Бриф не составлен: неверный ключ API Claude." in text


def test_link_that_is_not_media_is_reported_not_analyzed(settings, fake):
    c = make_client(settings)
    post(c, event(f"http://127.0.0.1:{fake.port}/cdn/page"))
    assert not fake.by_path("/v1/messages")
    msg = fake.by_path("im.message.add")[0]["body"]["MESSAGE"]
    assert "Скачать медиа не удалось: по ссылке не видео и не картинка (text/html)." in msg
    assert c.app.state.store.get("mid-1:0")["status"] == "no_media"


def test_foreign_host_is_not_downloaded(settings, fake):
    c = make_client(replace(settings, media_allowed_hosts=("fbsbx.com",)))
    post(c, event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4"))
    assert not fake.by_path("/cdn/")
    msg = fake.by_path("im.message.add")[0]["body"]["MESSAGE"]
    assert "не входит в MEDIA_ALLOWED_HOSTS" in msg


def test_media_route_rejects_unknown_names(settings):
    c = make_client(settings)
    assert c.get("/m/short/video.mp4").status_code == 404
    assert c.get("/m/" + "a" * 22 + "/bot.db").status_code == 404
    assert c.get("/m/" + "a" * 22 + "/video.mp4").status_code == 404


# ------------------------------------------------------------------ служебные команды


def test_subscribe_command_enables_messages(settings, fake, capsys):
    from app import subscribe

    assert subscribe.run(settings) is True
    posts = [c for c in fake.calls if c["method"] == "POST" and "subscribed_apps" in c["path"]]
    assert len(posts) == 1 and "subscribed_fields=messages" in posts[0]["path"]
    out = capsys.readouterr().out
    assert "Включение подписки: HTTP 200" in out and "success" in out


def test_token_is_refreshed_only_when_old_enough(settings, fake, monkeypatch):
    import httpx

    seen = []

    def fake_get(url, params=None, timeout=None):
        seen.append((url, params))
        return httpx.Response(200, json={"access_token": "NEW-TOKEN", "expires_in": 5183944})

    monkeypatch.setattr(meta.httpx, "get", fake_get)
    assert meta.refresh_token_if_needed(settings) == "токен записан"
    assert meta.refresh_token_if_needed(settings) == "обновление не требуется"
    assert not seen
    assert meta.refresh_token_if_needed(settings, min_age_days=0) == "обновлён"
    assert seen[0][0].endswith("/refresh_access_token")
    assert seen[0][1]["grant_type"] == "ig_refresh_token"
    assert meta.current_token(settings) == "NEW-TOKEN"


# ------------------------------------------------------------------ бот в чате Битрикс24


def lead_with_id() -> list[dict]:
    return [{"ID": "15", "TITLE": "", "NAME": "", "LAST_NAME": "", "IM": [{"VALUE": "imol|x|111|1"}]}]


def test_connect_registers_bot_adds_it_to_chat_and_saves(settings, fake, capsys):
    from app import bitrix_setup

    assert bitrix_setup.cmd_connect(settings, "chat42", "reel_bot") == 0
    reg = fake.by_path("imbot.v2.Bot.register")[0]["body"]["fields"]
    assert reg["code"] == "reel_bot" and reg["eventMode"] == "fetch"
    assert reg["properties"]["name"] == "Рилс бот"
    assert 20 <= len(reg["botToken"]) <= 40
    add = fake.by_path("im.chat.user.add")[0]["body"]
    assert add == {"CHAT_ID": 42, "USERS": [501]}
    send = fake.by_path("imbot.v2.Chat.Message.send")[0]["body"]
    assert send["botId"] == 501 and send["dialogId"] == "chat42" and send["botToken"] == reg["botToken"]
    saved = bitrix_setup.load_bot(settings)
    assert saved == {"code": "reel_bot", "bot_token": reg["botToken"], "bot_id": 501,
                     "name": "Рилс бот", "dialog_id": "chat42"}
    assert "Бот подключён к чату" in capsys.readouterr().out


def test_connect_twice_keeps_the_same_token(settings, fake):
    from app import bitrix_setup

    bitrix_setup.cmd_connect(settings, "chat42", "reel_bot")
    bitrix_setup.cmd_connect(settings, "chat42", "reel_bot")
    tokens = {c["body"]["fields"]["botToken"] for c in fake.by_path("imbot.v2.Bot.register")}
    assert len(tokens) == 1


def test_connect_refuses_open_line_chat(settings, fake, capsys):
    from app import bitrix_setup

    fake.dialog = {"name": "Анна, Instagram", "type": "lines", "entity_type": "LINES"}
    assert bitrix_setup.cmd_connect(settings, "chat77", "reel_bot") == 1
    assert not fake.by_path("imbot.v2.Bot.register")
    assert "чат открытой линии" in capsys.readouterr().out


def test_connect_asks_for_manual_add_when_bot_cannot_join(settings, fake, capsys):
    from app import bitrix_setup

    fake.chat_add_fails = True
    assert bitrix_setup.cmd_connect(settings, "chat42", "reel_bot") == 1
    out = capsys.readouterr().out
    assert "Добавить участников" in out and "Рилс бот" in out
    assert "dialog_id" not in bitrix_setup.load_bot(settings)


def test_connect_rejects_malformed_chat_id(settings, fake, capsys):
    from app import bitrix_setup

    assert bitrix_setup.cmd_connect(settings, "Менеджеры", "reel_bot") == 1
    assert not fake.calls
    assert "chat123" in capsys.readouterr().out


def test_chats_command_lists_only_group_chats(settings, fake, capsys):
    from app import bitrix_setup

    fake.recent = [{"id": "chat5", "title": "Менеджеры Instagram", "type": "chat"},
                   {"id": "12", "title": "Иван Петров", "type": "user"}]
    assert bitrix_setup.cmd_chats(settings) == 0
    out = capsys.readouterr().out
    assert "chat5" in out and "Менеджеры Instagram" in out and "Иван" not in out
    params = fake.by_path("im.recent.list")[0]["body"]
    assert params["SKIP_OPENLINES"] == "Y"


def test_reel_is_posted_by_bot_with_frames_and_card_link(settings, fake):
    from app import bitrix_setup

    bitrix_setup.cmd_connect(settings, "chat42", "reel_bot")
    fake.calls.clear()
    fake.leads = lead_with_id()
    c = make_client(settings)
    post(c, event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4"))

    assert not fake.by_path("im.message.add")
    body = fake.by_path("imbot.v2.Chat.Message.send")[0]["body"]
    assert body["dialogId"] == "chat42" and body["botId"] == 501
    msg = body["fields"]["message"]
    assert "Товар: женская куртка бомбер" in msg
    assert msg.rstrip().endswith("/crm/lead/details/15/")
    blocks = body["fields"]["attach"]["BLOCKS"]
    images = blocks[0]["IMAGE"]
    assert len(images) == 3
    assert images[0]["LINK"].startswith("https://reels.example.kz/m/") and images[0]["LINK"].endswith("/frame_01.jpg")
    assert blocks[1]["LINK"]["NAME"] == "Смотреть ролик" and blocks[1]["LINK"]["LINK"].endswith("/video.mp4")
    # ссылки на кадры из карточки действительно открываются
    frame_path = images[0]["LINK"].replace("https://reels.example.kz", "")
    assert c.get(frame_path).headers["content-type"] == "image/jpeg"
    d = c.app.state.store.get("mid-1:0")["details"]["delivery"]
    assert d["chat_sender"] == "bot" and d["chat_attach"] is True and d["target"] == "timeline"


def test_rejected_attach_is_retried_as_plain_text(settings, fake):
    from app import bitrix_setup

    bitrix_setup.cmd_connect(settings, "chat42", "reel_bot")
    fake.calls.clear()
    fake.attach_fail = True
    c = make_client(settings)
    post(c, event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4"))
    sends = fake.by_path("imbot.v2.Chat.Message.send")
    assert len(sends) == 2 and "attach" not in sends[1]["body"]["fields"]
    assert c.app.state.store.get("mid-1:0")["details"]["delivery"]["chat_attach"] is False


def test_bot_removed_from_chat_falls_back_to_webhook_user(settings, fake):
    from app import bitrix_setup

    bitrix_setup.cmd_connect(settings, "chat42", "reel_bot")
    fake.calls.clear()
    fake.bot_in_chat = False
    c = make_client(settings)
    post(c, event(f"http://127.0.0.1:{fake.port}/cdn/reel.mp4"))
    fallback = fake.by_path("im.message.add")
    assert len(fallback) == 1 and fallback[0]["body"]["DIALOG_ID"] == "chat42"
    assert fallback[0]["body"]["ATTACH"]["BLOCKS"][0]["IMAGE"]
    assert c.app.state.store.get("mid-1:0")["details"]["delivery"]["chat_sender"] == "webhook_user"


def test_privacy_pages(settings):
    c = make_client(settings)
    assert c.get("/privacy").status_code == 404
    c = make_client(replace(settings, company_name="ТОО <Тест>", contact_email="info@example.kz"))
    r = c.get("/privacy")
    assert r.status_code == 200 and "ТОО &lt;Тест&gt;" in r.text and "info@example.kz" in r.text
    assert c.get("/data-deletion").status_code == 200


def test_page_token_is_not_refreshed_and_subscribes_page(settings, fake, monkeypatch, capsys):
    """Единый вход: токен Страницы (EAA...) не обновляется, /me запрашивается с полями Страницы."""
    from app import subscribe

    page = replace(settings, ig_access_token="EAAG-page-token-000000000000")
    monkeypatch.setattr(meta.httpx, "get", lambda *a, **k: pytest.fail("токен Страницы не должен обновляться"))
    assert meta.refresh_token_if_needed(page, min_age_days=0) == "обновление не требуется"
    assert meta.current_token(page) == "EAAG-page-token-000000000000"

    assert subscribe.run(page) is True
    me = [c for c in fake.calls if c["method"] == "GET" and c["path"].startswith("/v27.0/me?")]
    assert "instagram_business_account" in me[0]["path"] and "user_id" not in me[0]["path"]


def test_graph_host_follows_token_kind(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("IG_GRAPH_HOST", raising=False)
    monkeypatch.setenv("IG_ACCESS_TOKEN", "EAAG-page")
    assert Settings.from_env().ig_graph_host == "https://graph.facebook.com"
    monkeypatch.setenv("IG_ACCESS_TOKEN", "IGAA-insta")
    assert Settings.from_env().ig_graph_host == "https://graph.instagram.com"
