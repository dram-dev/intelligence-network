"""Config loader — reads .env via pydantic-settings, exposes typed Settings."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: Path = Field(default=Path("./data/network.db"), alias="DB_PATH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ── Telegram: the primary push + pull channel ──────────────────────────
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_admin_chat_id: str = Field(default="", alias="TELEGRAM_ADMIN_CHAT_ID")
    notify_enabled: bool = Field(default=True, alias="NOTIFY_ENABLED")
    # Quiet hours gate ONLY the daily digest ping. Warnings/events are opt-in.
    notify_quiet_start_hour: int = Field(default=22, alias="NOTIFY_QUIET_START_HOUR")
    notify_quiet_end_hour: int = Field(default=8, alias="NOTIFY_QUIET_END_HOUR")

    # The bot's public @handle (from BotFather) — shown on the site + in the digest.
    telegram_bot_handle: str = Field(default="", alias="TELEGRAM_BOT_HANDLE")
    # Optional community links surfaced on the site.
    telegram_group_url: str = Field(default="", alias="TELEGRAM_GROUP_URL")
    github_repo: str = Field(default="dram-dev/intelligence-network", alias="GITHUB_REPO")
    site_url: str = Field(default="", alias="SITE_URL")
    # After each daily run, rebuild docs/ (the public site + JSON snapshot) and,
    # when true, commit + push it so GitHub Pages stays current.
    site_auto_push: bool = Field(default=False, alias="SITE_AUTO_PUSH")

    # ── Network policy ────────────────────────────────────────────────────
    network_name: str = Field(default="Intelligence Network", alias="NETWORK_NAME")
    network_join_code: str = Field(default="", alias="NETWORK_JOIN_CODE")
    network_rate_limit: int = Field(default=20, alias="NETWORK_RATE_LIMIT")
    event_push_min_score: float = Field(default=1.0, alias="EVENT_PUSH_MIN_SCORE")

    # ── Geography ─────────────────────────────────────────────────────────
    geo_state: str = Field(default="IL", alias="GEO_STATE")
    # Clock times typed by contributors ("at 3:15pm") are in this zone.
    local_tz: str = Field(default="America/Chicago", alias="LOCAL_TZ")
    geo_online_lookup: bool = Field(default=True, alias="GEO_ONLINE_LOOKUP")
    nws_user_agent: str = Field(
        default="intelligence-network (set NWS_USER_AGENT)", alias="NWS_USER_AGENT"
    )

    # ── Reference feeds ───────────────────────────────────────────────────
    station_poll_minutes: int = Field(default=60, alias="STATION_POLL_MINUTES")
    lsr_lookback_hours: int = Field(default=3, alias="LSR_LOOKBACK_HOURS")
    reference_retention_days: int = Field(default=30, alias="REFERENCE_RETENTION_DAYS")
    news_enabled: bool = Field(default=True, alias="NEWS_ENABLED")

    # ── Local LLMs (shared Mac-mini servers; see digest_core.summarize) ───
    llm_enabled: bool = Field(default=True, alias="LLM_ENABLED")
    parser_backend: str = Field(default="local_qwen", alias="PARSER_BACKEND")
    triage_backend: str = Field(default="local_qwen", alias="TRIAGE_BACKEND")
    summarizer_backend: str = Field(default="mlx_local", alias="SUMMARIZER_BACKEND")
    summarizer_timeout_sec: int = Field(default=120, alias="SUMMARIZER_TIMEOUT_SEC")
    ollama_host: str = Field(default="http://localhost:11434", alias="OLLAMA_HOST")
    ollama_model: str = Field(default="qwen3.6:35b-a3b", alias="OLLAMA_MODEL")
    ollama_think: bool | None = Field(default=None, alias="OLLAMA_THINK")
    mlx_server_url: str = Field(default="http://localhost:8080", alias="MLX_SERVER_URL")
    mlx_model: str = Field(default="mlx-community/Qwen3.6-27B-4bit", alias="MLX_MODEL")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")

    # ── Google Drive: where the digest lives ──────────────────────────────
    gdrive_enabled: bool = Field(default=True, alias="GDRIVE_ENABLED")
    gdrive_credentials_path: Path = Field(
        default=Path("./secrets/gdrive_credentials.json"), alias="GDRIVE_CREDENTIALS_PATH"
    )
    gdrive_token_path: Path = Field(
        default=Path("./secrets/gdrive_token.json"), alias="GDRIVE_TOKEN_PATH"
    )
    gdrive_folder_name: str = Field(default="Intelligence Network", alias="GDRIVE_FOLDER_NAME")
    gdrive_public_link: bool = Field(default=True, alias="GDRIVE_PUBLIC_LINK")

    @field_validator("ollama_host", "mlx_server_url", mode="before")
    @classmethod
    def _validate_localhost_url(cls, v: str) -> str:
        hostname = urlparse(str(v)).hostname
        if hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError(f"URL must point to localhost for safety, got hostname: {hostname!r}")
        return v

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def _strip_bot_prefix(cls, v: str) -> str:
        """Tolerate a token pasted with the URL's 'bot' prefix (.../bot<TOKEN>)."""
        v = str(v).strip()
        if re.match(r"(?i)^bot\d", v):
            v = v[3:]
        return v

    @field_validator("geo_state", mode="before")
    @classmethod
    def _upper_state(cls, v: str) -> str:
        return str(v).strip().upper()


settings = Settings()
