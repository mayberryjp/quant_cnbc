"""Slice 1 tests: domain models, LLM output schemas, repositories."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.models.domain import (
    DeliveryStatus,
    Distillation,
    ReferencedEntity,
    Sentiment,
    Transcript,
    TranscriptStatus,
    WatchlistStatus,
)
from app.models.llm_schemas import DistillOutput, EntityOutput, SentimentOutput
from app.models.requests import ReprocessRequest


# ---------------------------------------------------------------------------
# Models (no database)
# ---------------------------------------------------------------------------
class TestModels:
    def test_transcript_defaults(self):
        t = Transcript(
            archive_identifier="CNBC_20260702_220000_Mad_Money",
            air_date=date(2026, 7, 2),
            source_url="https://archive.org/details/CNBC_20260702_220000_Mad_Money",
        )
        assert t.status == TranscriptStatus.discovered
        assert t.attempts == 0

    def test_distill_output_parses(self):
        out = DistillOutput.model_validate(
            {"summary": "s", "key_topics": ["ai"], "segments": [{"speaker": "Cramer", "summary": "x"}]}
        )
        assert out.summary == "s"
        assert out.segments[0].speaker == "Cramer"

    def test_sentiment_output_clamps_and_labels(self):
        out = SentimentOutput.model_validate(
            {"observations": [
                {"subject": "AAPL", "sentiment_label": "bullish", "sentiment_score": 2.0, "confidence": -1},
            ]}
        )
        obs = out.observations[0]
        assert obs.sentiment_score == 1.0     # clamped to [-1, 1]
        assert obs.confidence == 0.0          # clamped to [0, 1]

    def test_entity_output_uppercases_ticker(self):
        out = EntityOutput.model_validate(
            {"entities": [
                {"raw_mention": "Apple", "entity_type": "company", "company_name": "Apple Inc.", "ticker": "aapl"},
            ]}
        )
        assert out.entities[0].ticker == "AAPL"

    def test_reprocess_request_from_alias(self):
        req = ReprocessRequest.model_validate({"show": "mad-money", "from": "sentiment", "only_stale": True})
        assert req.from_stage == "sentiment"
        assert req.only_stale is True


# ---------------------------------------------------------------------------
# Repositories (require Postgres; skipped otherwise via clean_db -> db_engine)
# ---------------------------------------------------------------------------
def _make_transcript(**over):
    base = dict(
        archive_identifier="CNBC_20260702_220000_Mad_Money",
        show_slug="Mad_Money",
        air_date=date(2026, 7, 2),
        broadcast_start=datetime(2026, 7, 2, 22, 0, tzinfo=timezone.utc),
        title="Mad Money",
        source_url="https://archive.org/details/CNBC_20260702_220000_Mad_Money",
        archive_addeddate=datetime(2026, 7, 2, 22, 0, tzinfo=timezone.utc),
    )
    base.update(over)
    return base


class TestRepositories:
    def test_discover_dedup_and_fetch(self, clean_db):
        from app.repository.transcripts import TranscriptRepository

        repo = TranscriptRepository(clean_db)
        assert repo.upsert_discovered([_make_transcript()]) == 1
        assert repo.upsert_discovered([_make_transcript()]) == 0  # dedup on identifier

        t = repo.get_by_identifier("CNBC_20260702_220000_Mad_Money")
        assert t is not None and t.status == TranscriptStatus.discovered

        repo.mark_fetched(t.id, raw_text="hello world", content_hash="abc123", caption_file="x.srt")
        t2 = repo.get_by_id(t.id)
        assert t2.status == TranscriptStatus.fetched
        assert t2.raw_text == "hello world"

    def test_distillation_versioning(self, clean_db):
        from app.repository.distillations import DistillationRepository
        from app.repository.transcripts import TranscriptRepository

        troepo = TranscriptRepository(clean_db)
        troepo.upsert_discovered([_make_transcript()])
        t = troepo.get_by_identifier("CNBC_20260702_220000_Mad_Money")

        drepo = DistillationRepository(clean_db)
        drepo.upsert(Distillation(transcript_id=t.id, model="m1", prompt_version="v1", summary="old"))
        drepo.upsert(Distillation(transcript_id=t.id, model="m2", prompt_version="v1", summary="new"))

        current = drepo.get_current(t.id)
        assert current.summary == "new"  # newest wins
        assert current.model == "m2"

    def test_sentiment_idempotent_insert(self, clean_db):
        from app.repository.sentiments import SentimentRepository
        from app.repository.transcripts import TranscriptRepository

        troepo = TranscriptRepository(clean_db)
        troepo.upsert_discovered([_make_transcript()])
        t = troepo.get_by_identifier("CNBC_20260702_220000_Mad_Money")

        srepo = SentimentRepository(clean_db)
        s = Sentiment(
            transcript_id=t.id, subject="AAPL", sentiment_label="bullish",
            model="m1", prompt_version="v1",
            idempotency_key="cnbc:CNBC_20260702_220000_Mad_Money:AAPL:m1:v1",
        )
        id1 = srepo.insert(s)
        id2 = srepo.insert(s)  # same idempotency key
        assert id1 == id2
        srepo.set_delivery(id1, DeliveryStatus.sent, sentiment_id="sentiment:cnbc:x",
                           delivered_at=datetime.now(timezone.utc))
        items, total = srepo.list(subject="AAPL")
        assert total == 1 and items[0].delivery_status == DeliveryStatus.sent

    def test_entity_idempotent_and_cursor(self, clean_db):
        from app.repository.entities import EntityRepository
        from app.repository.runs import RunRepository
        from app.repository.transcripts import TranscriptRepository

        troepo = TranscriptRepository(clean_db)
        troepo.upsert_discovered([_make_transcript()])
        t = troepo.get_by_identifier("CNBC_20260702_220000_Mad_Money")

        erepo = EntityRepository(clean_db)
        e = ReferencedEntity(
            transcript_id=t.id, raw_mention="Apple", entity_type="company",
            company_name="Apple Inc.", ticker="AAPL", model="m1", prompt_version="v1",
            idempotency_key="cnbc:CNBC_20260702_220000_Mad_Money:AAPL:m1:v1",
        )
        assert erepo.insert(e) == erepo.insert(e)
        erepo.set_watchlist(erepo.insert(e), WatchlistStatus.submitted,
                            submitted_at=datetime.now(timezone.utc))

        rrepo = RunRepository(clean_db)
        rrepo.set_cursor("TV-CNBC", last_addeddate=datetime(2026, 7, 2, tzinfo=timezone.utc),
                         last_identifier="CNBC_20260702_220000_Mad_Money")
        cur = rrepo.get_cursor("TV-CNBC")
        assert cur.last_identifier == "CNBC_20260702_220000_Mad_Money"

    def test_run_counters_and_stats(self, clean_db):
        from app.repository.runs import RunRepository

        rrepo = RunRepository(clean_db)
        rrepo.start_run(date(2026, 7, 3))
        rrepo.add_counters(date(2026, 7, 3), transcripts_fetched=2, distilled=2, sentiments_sent=5)
        rrepo.complete_run(date(2026, 7, 3), "success")
        stats = rrepo.stats()
        assert stats["transcripts_fetched"] == 2
        assert stats["sentiments_sent"] == 5
        assert stats["last_run_status"] == "success"
