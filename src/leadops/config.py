"""Configuration. Every secret is read from the environment; nothing is defaulted
to a real value and nothing is written back to disk.

`.env.example` documents each variable. The service starts with no credentials at
all: the AI provider falls back to `deterministic` and the GHL client points at the
local mock. That is what makes the demo runnable by a reviewer.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AIProvider = Literal["deterministic", "anthropic", "openai"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix=""
    )

    # ---- service ----------------------------------------------------------
    environment: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    # ---- storage ----------------------------------------------------------
    # SQLite by default so the demo needs no services. See docs/architecture.md
    # for why Postgres is the right choice once more than one worker runs.
    database_url: str = "sqlite+pysqlite:///./local.sqlite3"

    # ---- webhook security -------------------------------------------------
    webhook_signing_secret: str = ""
    # Reject signed requests whose timestamp is older than this (replay window).
    webhook_max_skew_seconds: int = 300
    # When no secret is configured the service refuses to *pretend* it verified
    # anything: it accepts the request but marks it unverified in the audit log.
    require_signature: bool = False

    # ---- GoHighLevel ------------------------------------------------------
    # Point this at the bundled mock (default) or at services.leadconnectorhq.com.
    ghl_base_url: str = "http://127.0.0.1:8081"
    ghl_api_version: str = "v3"
    ghl_access_token: str = ""
    ghl_location_id: str = "loc_DEMO0000000000000000"
    ghl_pipeline_id: str = "pipe_DEMO000000000000000"
    ghl_timeout_seconds: float = 10.0
    ghl_max_attempts: int = 4
    ghl_backoff_base_seconds: float = 0.25
    ghl_backoff_max_seconds: float = 8.0

    # ---- AI ---------------------------------------------------------------
    ai_provider: AIProvider = "deterministic"
    ai_model: str = "claude-sonnet-5"
    ai_api_key: str = ""
    ai_timeout_seconds: float = 20.0
    ai_max_attempts: int = 2  # 1 call + 1 schema-repair call
    ai_temperature: float = 0.0

    # ---- routing ----------------------------------------------------------
    routing_config_path: str = "config/routing.yml"
    ghl_mapping_path: str = "config/ghl-mapping.json"

    # ---- reliability ------------------------------------------------------
    # An event left in `processing` longer than this is considered abandoned by a
    # crashed worker and may be reclaimed.
    processing_lease_seconds: int = 300
    max_delivery_attempts: int = 5

    sales_notification_email: str = "sales@example.com"

    @field_validator("ghl_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def ai_enabled_live(self) -> bool:
        """True only when a real provider is configured *and* has a key."""
        return self.ai_provider != "deterministic" and bool(self.ai_api_key)

    def redacted(self) -> dict:
        """Safe-to-log view. Used by /healthz so operators can confirm what is
        configured without exposing the values."""
        return {
            "environment": self.environment,
            "database": self.database_url.split("://", 1)[0],
            "ghl_base_url": self.ghl_base_url,
            "ghl_token_configured": bool(self.ghl_access_token),
            "ai_provider": self.ai_provider,
            "ai_key_configured": bool(self.ai_api_key),
            "signature_required": self.require_signature,
            "signing_secret_configured": bool(self.webhook_signing_secret),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Tests change the environment between cases; the cache has to go with it."""
    get_settings.cache_clear()
