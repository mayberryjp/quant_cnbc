"""Domain models + enums for quant_cnbc (Pydantic v2)."""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class TranscriptStatus(str, enum.Enum):
    discovered = "discovered"
    fetched = "fetched"
    distilled = "distilled"
    delivered = "delivered"
    done = "done"
    failed = "failed"


class DeliveryStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    duplicate = "duplicate"
    failed = "failed"


class WatchlistStatus(str, enum.Enum):
    pending = "pending"
    submitted = "submitted"
    duplicate = "duplicate"
    failed = "failed"
    unresolved = "unresolved"


class SentimentLabel(str, enum.Enum):
    bullish = "bullish"
    bearish = "bearish"
    neutral = "neutral"


class SubjectType(str, enum.Enum):
    ticker = "ticker"
    sector = "sector"
    theme = "theme"
    market = "market"


class EntityType(str, enum.Enum):
    ticker = "ticker"
    company = "company"


class Direction(str, enum.Enum):
    long = "long"
    short = "short"
    neutral = "neutral"


class RunStatus(str, enum.Enum):
    running = "running"
    success = "success"
    partial = "partial"
    failed = "failed"


# ---------------------------------------------------------------------------
# Domain records
# ---------------------------------------------------------------------------
class Show(BaseModel):
    id: int | None = None
    slug: str
    display_name: str
    archive_query: str | None = None
    enabled: bool = True


class Transcript(BaseModel):
    id: int | None = None
    archive_identifier: str
    show_id: int | None = None
    show_slug: str | None = None
    air_date: date
    broadcast_start: datetime | None = None
    title: str | None = None
    source_url: str
    caption_file: str | None = None
    content_hash: str | None = None
    raw_text: str | None = None
    status: TranscriptStatus = TranscriptStatus.discovered
    attempts: int = 0
    last_error: str | None = None
    archive_addeddate: datetime | None = None
    discovered_at: datetime | None = None
    fetched_at: datetime | None = None
    distilled_at: datetime | None = None
    delivered_at: datetime | None = None


class Distillation(BaseModel):
    id: int | None = None
    transcript_id: int
    model: str
    prompt_version: str
    summary: str
    key_topics: list[str] = Field(default_factory=list)
    segments: list[dict[str, Any]] = Field(default_factory=list)
    token_usage: dict[str, Any] | None = None
    is_current: bool = True
    created_at: datetime | None = None


class Sentiment(BaseModel):
    id: int | None = None
    transcript_id: int
    subject_type: SubjectType = SubjectType.ticker
    subject: str
    sentiment_label: SentimentLabel
    sentiment_score: float | None = None
    confidence: float | None = None
    horizon: str | None = None
    reason: str | None = None
    model: str
    prompt_version: str
    idempotency_key: str
    delivery_status: DeliveryStatus = DeliveryStatus.pending
    sentiment_id: str | None = None
    delivered_at: datetime | None = None
    created_at: datetime | None = None


class ReferencedEntity(BaseModel):
    id: int | None = None
    transcript_id: int
    raw_mention: str
    entity_type: EntityType
    company_name: str | None = None
    ticker: str | None = None
    speaker: str | None = None
    direction: Direction | None = None
    confidence: float | None = None
    context: str | None = None
    model: str
    prompt_version: str
    idempotency_key: str
    watchlist_status: WatchlistStatus = WatchlistStatus.pending
    submitted_at: datetime | None = None
    created_at: datetime | None = None


class IngestRun(BaseModel):
    id: int | None = None
    run_date: date
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: RunStatus = RunStatus.running
    shows_processed: int = 0
    transcripts_fetched: int = 0
    distilled: int = 0
    reprocessed: int = 0
    sentiments_sent: int = 0
    entities_submitted: int = 0
    failures: int = 0
    heartbeat_at: datetime | None = None


class IngestCursor(BaseModel):
    collection: str
    last_addeddate: datetime | None = None
    last_identifier: str | None = None
    updated_at: datetime | None = None
