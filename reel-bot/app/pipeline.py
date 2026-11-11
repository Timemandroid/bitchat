"""Обработка одного вложения: скачать, нарезать, составить бриф, передать в Битрикс24."""
from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path

import anthropic

from . import brief as brief_mod
from . import frames as frames_mod
from . import meta
from .bitrix import Bitrix, BitrixError, Match
from .config import Settings
from .store import Store

log = logging.getLogger("reelbot.pipeline")

TYPE_RU = {"ig_reel": "рилс", "reel": "рилс", "ig_post": "пост", "share": "пост"}


class Pipeline:
    def __init__(self, settings: Settings, store: Store, bitrix: Bitrix, claude: anthropic.Anthropic | None):
        self.s = settings
        self.store = store
        self.bitrix = bitrix
        self.claude = claude

    # ------------------------------------------------------------ текст для CRM

    def _compose(self, item: meta.Incoming, profile: dict, media: meta.Media, video_url: str,
                 duration: float, brief_lines: list[str]) -> str:
        what = TYPE_RU.get(item.type, "вложение")
        who = profile.get("name") or ""
        if profile.get("username"):
            who = f"{who} (@{profile['username']})".strip()
        if not who:
            who = f"ID {item.sender_id}"
        lines = [f"Клиент прислал {what} в Instagram", f"От: {who}"]
        if media.kind == "video" and video_url:
            lines.append(f"Смотреть ролик ({duration:.0f} с): {video_url}")
        elif media.kind == "image" and video_url:
            lines.append(f"Смотреть картинку: {video_url}")
        else:
            lines.append(f"Скачать медиа не удалось: {media.error}.")
            if item.url:
                lines.append(f"Исходная ссылка (может быстро устареть): {item.url}")
        if item.title:
            lines.append(f"Подпись автора: {item.title[:600]}")
        if item.text:
            lines.append(f"Сообщение клиента: {item.text[:600]}")
        if brief_lines:
            lines.append("")
            lines.extend(brief_lines)
        return "\n".join(lines)

    # ------------------------------------------------------------ доставка

    def _deliver(self, item: meta.Incoming, profile: dict, text: str, frame_paths: list[Path],
                 video_path: Path | None) -> dict:
        out: dict = {"target": "none"}
        if not self.bitrix.enabled:
            out["note"] = "BITRIX_WEBHOOK_URL не задан, результат только в журнале"
            return out

        names = [profile.get("name", ""), profile.get("username", "")]
        match: Match | None = None
        for attempt in range(max(1, self.s.bitrix_match_retries)):
            try:
                match, info = self.bitrix.find_entity(item.sender_id, names)
                out["match_info"] = info
            except BitrixError as e:
                out["match_error"] = str(e)
                log.error("поиск клиента в Битрикс24 не удался: %s", e)
                break
            if match:
                break
            # Битрикс24 мог ещё не создать лид по этому сообщению
            if attempt + 1 < self.s.bitrix_match_retries:
                time.sleep(self.s.bitrix_match_retry_sec)

        if match:
            files = list(frame_paths[: self.s.bitrix_attach_frames])
            with_video = bool(
                video_path and video_path.stat().st_size <= self.s.bitrix_attach_video_max_mb * 1024 * 1024
            )
            try:
                try:
                    cid = self.bitrix.add_comment(match, text, files + ([video_path] if with_video else []))
                except BitrixError as e:
                    if not with_video:
                        raise
                    log.warning("комментарий с видео не принят (%s), повтор без видео", e)
                    with_video = False
                    cid = self.bitrix.add_comment(match, text, files)
                out.update(target="timeline", entity=f"{match.entity_type}:{match.entity_id}",
                           matched_by=match.how, comment_id=cid, video_attached=with_video)
            except BitrixError as e:
                out["comment_error"] = str(e)
                log.error("комментарий в ленту не добавлен: %s", e)
                match = None

        if self.s.bitrix_fallback_dialog_id:
            if match:
                msg = f"Клиент прислал ролик в Instagram, бриф лежит в карточке: {match.url}"
            else:
                msg = "Карточка клиента не найдена автоматически.\n" + text
            try:
                out["chat_message_id"] = self.bitrix.send_message(self.s.bitrix_fallback_dialog_id, msg)
                if not match:
                    out["target"] = "chat"
            except BitrixError as e:
                out["chat_error"] = str(e)
                log.error("сообщение в чат не отправлено: %s", e)
        return out

    # ------------------------------------------------------------ основной шаг

    def process(self, item: meta.Incoming) -> None:
        started = time.time()
        details: dict = {"type": item.type, "sender": item.sender_id, "channel": item.channel}
        status = "error"
        try:
            token = secrets.token_urlsafe(16)
            work = self.s.media_dir / token
            profile = meta.fetch_profile(self.s, item.sender_id)
            details["profile_found"] = bool(profile)

            media = meta.download_media(self.s, item.url, work)
            details["media"] = {"kind": media.kind, "size": media.size, "content_type": media.content_type,
                                "error": media.error}

            frames: list[frames_mod.Frame] = []
            duration = 0.0
            video_url = ""
            if media.path is not None:
                video_url = f"{self.s.public_base_url}/m/{token}/{media.path.name}"
                if media.kind == "video":
                    frames, duration = frames_mod.extract_frames(media.path, work, self.s.frames, self.s.frame_max_side)
                else:
                    frames = frames_mod.normalize_image(media.path, work, self.s.frame_max_side)
            details["frames"] = len(frames)
            details["duration"] = round(duration, 1)
            details["url"] = video_url

            brief_lines: list[str] = []
            if frames and self.s.brief_enabled and self.claude is not None:
                result = brief_mod.make_brief(self.s, self.claude, frames, item.title, item.text, duration, media.kind)
                brief_lines = brief_mod.format_brief(result)
                details["brief"] = result.data
                details["brief_error"] = result.error
                details["brief_model"] = result.model
                details["brief_usage"] = result.usage

            text = self._compose(item, profile, media, video_url, duration, brief_lines)
            details["text"] = text
            video_path = media.path if media.kind == "video" else None
            details["delivery"] = self._deliver(item, profile, text, [f.path for f in frames], video_path)
            status = "done" if media.path is not None else "no_media"
        except Exception as e:  # обработка идёт в фоне, поэтому ошибку нужно записать, а не потерять
            log.exception("обработка %s не удалась", item.key)
            details["exception"] = f"{e.__class__.__name__}: {e}"
        details["seconds"] = round(time.time() - started, 1)
        self.store.finish(item.key, status, details)
        log.info("готово %s: %s за %s с, доставка: %s", item.key, status, details["seconds"],
                 (details.get("delivery") or {}).get("target"))
