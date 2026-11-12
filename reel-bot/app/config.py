"""Настройки бота. Все значения читаются из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    # Общее
    data_dir: Path = Path("/data")
    public_base_url: str = ""  # например https://reels.example.kz

    # Meta / Instagram
    meta_verify_token: str = ""
    meta_app_secrets: tuple[str, ...] = ()
    verify_signature: bool = True
    ig_access_token: str = ""
    ig_graph_host: str = "https://graph.instagram.com"
    ig_api_version: str = "v27.0"
    process_types: tuple[str, ...] = ("ig_reel", "reel", "ig_post", "share")
    media_allowed_hosts: tuple[str, ...] = (
        "fbsbx.com",
        "fbcdn.net",
        "cdninstagram.com",
        "instagram.com",
        "facebook.com",
    )
    media_allow_http: bool = False  # только для локальных тестов
    max_media_mb: int = 200
    log_raw_events: bool = False

    # Кадры
    frames: int = 8
    frame_max_side: int = 960

    # Claude
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    claude_fallbacks: bool = True
    claude_base_url: str = ""  # пусто = официальный адрес API
    anthropic_api_key: str = ""  # пусто = SDK сам возьмёт ANTHROPIC_API_KEY
    brief_enabled: bool = True
    brief_language: str = "русский"
    catalog_path: str = ""

    # Битрикс24
    bitrix_webhook_url: str = ""
    bitrix_entity_types: tuple[str, ...] = ("lead", "contact")
    bitrix_match_window_min: int = 30
    bitrix_match_retries: int = 3
    bitrix_match_retry_sec: float = 5.0
    bitrix_dialog_id: str = ""  # чат менеджеров, например chat123
    bitrix_chat_full: bool = True  # в чат всегда уходит полный бриф, а не только уведомление
    bitrix_bot_name: str = "Рилс бот"
    bitrix_attach_video_max_mb: int = 15
    bitrix_attach_frames: int = 3

    # Хранение
    media_retention_days: int = 30

    # Страницы для Meta
    company_name: str = ""
    contact_email: str = ""

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def bot_file(self) -> Path:
        return self.data_dir / "bitrix_bot.json"

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            data_dir=Path(e("DATA_DIR", "/data")),
            public_base_url=e("PUBLIC_BASE_URL", "").rstrip("/"),
            meta_verify_token=e("META_VERIFY_TOKEN", ""),
            meta_app_secrets=_list("META_APP_SECRET", ""),
            verify_signature=_bool("VERIFY_SIGNATURE", True),
            ig_access_token=e("IG_ACCESS_TOKEN", ""),
            ig_graph_host=e("IG_GRAPH_HOST", "https://graph.instagram.com").rstrip("/"),
            ig_api_version=e("IG_API_VERSION", "v27.0"),
            process_types=_list("PROCESS_TYPES", "ig_reel,reel,ig_post,share"),
            media_allowed_hosts=_list(
                "MEDIA_ALLOWED_HOSTS",
                "fbsbx.com,fbcdn.net,cdninstagram.com,instagram.com,facebook.com",
            ),
            media_allow_http=_bool("MEDIA_ALLOW_HTTP", False),
            max_media_mb=int(e("MAX_MEDIA_MB", "200")),
            log_raw_events=_bool("LOG_RAW_EVENTS", False),
            frames=int(e("FRAMES", "8")),
            frame_max_side=int(e("FRAME_MAX_SIDE", "960")),
            claude_model=e("CLAUDE_MODEL", "claude-opus-5-5"),
            claude_effort=e("CLAUDE_EFFORT", "medium"),
            claude_fallbacks=_bool("CLAUDE_FALLBACKS", True),
            claude_base_url=e("CLAUDE_BASE_URL", ""),
            anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
            brief_enabled=_bool("BRIEF_ENABLED", True),
            brief_language=e("BRIEF_LANGUAGE", "русский"),
            catalog_path=e("CATALOG_PATH", ""),
            bitrix_webhook_url=e("BITRIX_WEBHOOK_URL", "").rstrip("/"),
            bitrix_entity_types=_list("BITRIX_ENTITY_TYPES", "lead,contact"),
            bitrix_match_window_min=int(e("BITRIX_MATCH_WINDOW_MIN", "30")),
            bitrix_match_retries=int(e("BITRIX_MATCH_RETRIES", "3")),
            bitrix_match_retry_sec=float(e("BITRIX_MATCH_RETRY_SEC", "5")),
            bitrix_dialog_id=e("BITRIX_DIALOG_ID", "") or e("BITRIX_FALLBACK_DIALOG_ID", ""),
            bitrix_chat_full=_bool("BITRIX_CHAT_FULL", True),
            bitrix_bot_name=e("BITRIX_BOT_NAME", "Рилс бот"),
            bitrix_attach_video_max_mb=int(e("BITRIX_ATTACH_VIDEO_MAX_MB", "15")),
            bitrix_attach_frames=int(e("BITRIX_ATTACH_FRAMES", "3")),
            media_retention_days=int(e("MEDIA_RETENTION_DAYS", "30")),
            company_name=e("COMPANY_NAME", ""),
            contact_email=e("CONTACT_EMAIL", ""),
        )
