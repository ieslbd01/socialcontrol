"""Application settings, loaded from environment / .env (never from code)."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("SC_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # GitHub Actions passes unset secrets as empty strings: treat them as "not set"
        env_ignore_empty=True,
    )

    sc_env: str = "local"
    database_url: str = (
        "postgresql+psycopg://socialcontrol:socialcontrol@127.0.0.1:5433/socialcontrol"
    )
    sc_master_key: str = ""
    sc_signing_key: str = ""
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_key: str = ""
    supabase_bucket: str = "media"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_email_to: str = ""
    sc_admin_email: str = ""
    sc_admin_password_hash: str = ""
    sc_totp_secret: str = ""
    media_dir: str = "media_store"
    sc_base_url: str = "http://127.0.0.1:8000"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    sc_backup_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
