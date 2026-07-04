"""Pydantic schemas for the structured JSON returned by each LLM pass.

The model does the extraction; the service only validates against these schemas
and applies light normalization (uppercase ticker, clamp scores).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.domain import Direction, EntityType, SentimentLabel, SubjectType


def _clamp(value: float | None, lo: float, hi: float) -> float | None:
    if value is None:
        return None
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Pass 1 — distillation
# ---------------------------------------------------------------------------
class Segment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    speaker: str | None = None
    role: str | None = None
    summary: str = ""


class DistillOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str
    key_topics: list[str] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pass 2 — sentiment
# ---------------------------------------------------------------------------
class SentimentObservation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subject_type: SubjectType = SubjectType.ticker
    subject: str
    sentiment_label: SentimentLabel
    sentiment_score: float | None = None
    confidence: float | None = None
    horizon: str | None = None
    reason: str | None = None

    @field_validator("subject")
    @classmethod
    def _norm_subject(cls, v: str) -> str:
        return v.strip()

    @field_validator("sentiment_score")
    @classmethod
    def _clamp_score(cls, v: float | None) -> float | None:
        return _clamp(v, -1.0, 1.0)

    @field_validator("confidence")
    @classmethod
    def _clamp_conf(cls, v: float | None) -> float | None:
        return _clamp(v, 0.0, 1.0)


class SentimentOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    observations: list[SentimentObservation] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pass 3 — referenced entities (tickers/companies), with company->ticker
# ---------------------------------------------------------------------------
class EntityMention(BaseModel):
    model_config = ConfigDict(extra="ignore")
    raw_mention: str
    entity_type: EntityType = EntityType.company
    company_name: str | None = None
    ticker: str | None = None
    speaker: str | None = None
    direction: Direction | None = None
    confidence: float | None = None
    context: str | None = None

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, v: str | None) -> str | None:
        v = (v or "").strip().upper()
        return v or None

    @field_validator("confidence")
    @classmethod
    def _clamp_conf(cls, v: float | None) -> float | None:
        return _clamp(v, 0.0, 1.0)


class EntityOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    entities: list[EntityMention] = Field(default_factory=list)
