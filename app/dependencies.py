"""Dependency helpers — construct repositories bound to the shared engine."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from app.db import get_engine
from app.repository.distillations import DistillationRepository
from app.repository.entities import EntityRepository
from app.repository.runs import RunRepository
from app.repository.sentiments import SentimentRepository
from app.repository.transcripts import TranscriptRepository


def transcript_repo(engine: Engine | None = None) -> TranscriptRepository:
    return TranscriptRepository(engine or get_engine())


def distillation_repo(engine: Engine | None = None) -> DistillationRepository:
    return DistillationRepository(engine or get_engine())


def sentiment_repo(engine: Engine | None = None) -> SentimentRepository:
    return SentimentRepository(engine or get_engine())


def entity_repo(engine: Engine | None = None) -> EntityRepository:
    return EntityRepository(engine or get_engine())


def run_repo(engine: Engine | None = None) -> RunRepository:
    return RunRepository(engine or get_engine())
