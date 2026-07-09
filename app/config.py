"""Application settings loaded from environment variables.

CNBC-specific settings use the ``CNBC_`` env prefix. ``DATABASE_URL``,
``API_PORT`` and ``API_LISTEN_ADDRESS`` are read unprefixed (explicit aliases),
matching the ``quant_signals`` convention so a shared Postgres/compose file
works across sibling services.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CNBC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Unprefixed (platform-shared) ---
    database_url: str = Field(
        default="", validation_alias=AliasChoices("DATABASE_URL")
    )
    api_listen_address: str = Field(
        default="0.0.0.0", validation_alias=AliasChoices("API_LISTEN_ADDRESS")
    )
    api_port: int = Field(default=8019, validation_alias=AliasChoices("API_PORT"))

    # --- Ingestion schedule ---
    ingest_wake_time: str = "06:00"          # local HH:MM to wake and process prior day
    ingest_interval: int = 86400             # seconds between runs (fallback cadence)
    ingest_lookback_days: int = 1
    # Comma-separated allow-list of show names/slugs; empty = whole collection.
    # Defaults to the CNBC market-focused programmes so CNBC Prime entertainment
    # reruns (e.g. The Profit, Shark Tank, American Greed) are excluded.
    shows: str = (
        "The Exchange,Squawk Box Europe,Mad Money,Fast Money,"
        "Closing Bell,Power Lunch,Squawk on the Street,Squawk Box"
    )

    # --- archive.org ---
    archive_base_url: str = "https://archive.org"
    archive_collection: str = "TV-CNBC"
    archive_rate_limit: float = 1.0          # min seconds between archive.org calls
    archive_overlap_hours: int = 24          # discovery cursor look-back overlap

    # --- Local LLM (three structured passes) ---
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "llama3.1:8b"
    llm_api_key: str = ""
    llm_timeout: int = 120
    llm_max_tokens: int = 8192
    llm_json_mode: bool = True
    distill_prompt_version: str = "v4"
    sentiment_prompt_version: str = "v1"
    entity_prompt_version: str = "v1"

    # --- Downstream: quant_sentiment ---
    sentiment_api_url: str = "http://localhost:8017/sentiment"
    sentiment_api_key: str = ""
    sentiment_timeout: int = 30

    # --- Downstream: quant_signals watchlist ---
    watchlist_api_url: str = "http://localhost:8016/signals"
    watchlist_api_key: str = ""
    watchlist_source: str = "cnbc"
    watchlist_signal_type: str = "cnbc_mention"
    watchlist_timeout: int = 30

    # --- Reprocessing ---
    reprocess_on_prompt_change: bool = False

    # --- Resilience ---
    http_retries: int = 3
    retry_backoff: float = 1.0               # base seconds for exponential backoff
    max_attempts: int = 5                    # per-item processing retry cap

    # --- Validation limits / paging ---
    max_reason_length: int = 2000
    max_page_size: int = 100
    default_page_size: int = 25

    @property
    def show_allowlist(self) -> list[str]:
        return [s.strip() for s in self.shows.split(",") if s.strip()]


settings = Settings()
