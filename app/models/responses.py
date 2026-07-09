"""API response schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class TranscriptResponse(BaseModel):
    id: int
    archive_identifier: str
    show_slug: str | None = None
    air_date: date
    broadcast_start: datetime | None = None
    title: str | None = None
    source_url: str
    status: str
    attempts: int = 0
    last_error: str | None = None
    discovered_at: datetime | None = None
    fetched_at: datetime | None = None
    distilled_at: datetime | None = None
    delivered_at: datetime | None = None


class DistillationResponse(BaseModel):
    id: int
    transcript_id: int
    model: str
    prompt_version: str
    summary: str
    key_topics: list[str] = []
    segments: list[dict[str, Any]] = []
    token_usage: dict[str, Any] | None = None
    is_current: bool = True
    created_at: datetime | None = None


class TranscriptDetailResponse(TranscriptResponse):
    distillation: DistillationResponse | None = None


class SentimentResponse(BaseModel):
    id: int
    transcript_id: int
    subject_type: str
    subject: str
    sentiment_label: str
    sentiment_score: float | None = None
    confidence: float | None = None
    horizon: str | None = None
    reason: str | None = None
    delivery_status: str
    sentiment_id: str | None = None
    created_at: datetime | None = None


class EntityResponse(BaseModel):
    id: int
    transcript_id: int
    raw_mention: str
    entity_type: str
    company_name: str | None = None
    ticker: str | None = None
    speaker: str | None = None
    direction: str | None = None
    confidence: float | None = None
    watchlist_status: str
    created_at: datetime | None = None


class ListResponse(BaseModel):
    items: list[Any]
    total: int
    page: int
    page_size: int


class ReadinessResponse(BaseModel):
    status: str
    database: str
    last_run_status: str | None = None
    heartbeat: str | None = None


class StatsResponse(BaseModel):
    transcripts_fetched: int = 0
    distilled: int = 0
    reprocessed: int = 0
    sentiments_sent: int = 0
    entities_submitted: int = 0
    failures: int = 0
    transcripts_total: int = 0
    last_run_date: str | None = None
    last_run_status: str | None = None
    last_heartbeat: str | None = None


class ReprocessResponse(BaseModel):
    status: str
    matched: int
    archive_identifiers: list[str] = []
