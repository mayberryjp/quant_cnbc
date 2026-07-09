"""Slice 6 tests: pipeline orchestration (in-memory) + worker wake timing."""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.models.domain import (
    DeliveryStatus,
    Distillation,
    Transcript,
    TranscriptStatus,
    WatchlistStatus,
)
from app.services.ingest_worker import seconds_until_wake
from app.services.pipeline import Pipeline


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------
class FakeTranscriptRepo:
    def __init__(self):
        self._by_id: dict[int, Transcript] = {}
        self._seq = 0

    def add_fetched(self, aid: str, text: str) -> Transcript:
        self._seq += 1
        t = Transcript(
            id=self._seq, archive_identifier=aid, show_slug="Mad_Money",
            air_date=date(2026, 7, 2),
            broadcast_start=datetime(2026, 7, 2, 22, 0, tzinfo=timezone.utc),
            source_url=f"https://archive.org/details/{aid}", raw_text=text,
            status=TranscriptStatus.fetched,
        )
        self._by_id[t.id] = t
        return t

    def get_by_id(self, tid):
        return self._by_id.get(tid)

    def set_status(self, tid, status, last_error=None, bump_attempts=False):
        self._by_id[tid].status = TranscriptStatus(status) if isinstance(status, str) else status

    def touch_stage(self, tid, stage):
        pass

    def reset_for_reprocess(self, tid):
        self._by_id[tid].status = TranscriptStatus.fetched
        return True

    def reset_full(self, tid):
        t = self._by_id[tid]
        t.status = TranscriptStatus.discovered
        t.raw_text = None
        return True

    def mark_fetched(self, tid, *, raw_text, content_hash, caption_file=None):
        t = self._by_id[tid]
        t.raw_text = raw_text
        t.content_hash = content_hash
        t.caption_file = caption_file
        t.status = TranscriptStatus.fetched

    def list_actionable(self, *, limit=200, max_attempts=5):
        return list(self._by_id.values())


class FakeDistillationRepo:
    def __init__(self):
        self.current: dict[int, Distillation] = {}

    def upsert(self, d: Distillation):
        d.id = 1
        self.current[d.transcript_id] = d
        return d

    def get_current(self, tid):
        return self.current.get(tid)


class FakeSentimentRepo:
    def __init__(self):
        self.rows = {}
        self._seq = 0

    def insert(self, s):
        for rid, existing in self.rows.items():
            if existing.idempotency_key == s.idempotency_key:
                return rid
        self._seq += 1
        self.rows[self._seq] = s
        return self._seq

    def set_delivery(self, rid, status, sentiment_id=None, delivered_at=None):
        self.rows[rid].delivery_status = status


class FakeEntityRepo:
    def __init__(self):
        self.rows = {}
        self._seq = 0

    def insert(self, e):
        for rid, existing in self.rows.items():
            if existing.idempotency_key == e.idempotency_key:
                return rid
        self._seq += 1
        self.rows[self._seq] = e
        return self._seq

    def set_watchlist(self, rid, status, submitted_at=None):
        self.rows[rid].watchlist_status = status


class FakeRunRepo:
    def __init__(self):
        self.counters = {}
        self.status = None

    def start_run(self, run_date):
        pass

    def add_counters(self, run_date, **c):
        for k, v in c.items():
            self.counters[k] = self.counters.get(k, 0) + v

    def complete_run(self, run_date, status):
        self.status = status

    def get_cursor(self, collection):
        return None

    def set_cursor(self, collection, *, last_addeddate, last_identifier):
        pass


class FakeLLM:
    def complete_json(self, system, user, json_schema=None):
        s = system.lower()
        if "market-sentiment" in s:
            return {"observations": [
                {"subject_type": "ticker", "subject": "AAPL", "sentiment_label": "bullish",
                 "sentiment_score": 0.8, "confidence": 0.7},
                {"subject_type": "market", "subject": "ALL", "sentiment_label": "neutral"},
            ]}, {}
        if "extract every company" in s:
            return {"entities": [
                {"raw_mention": "Apple", "entity_type": "company", "company_name": "Apple Inc.", "ticker": "AAPL"},
                {"raw_mention": "a private co", "entity_type": "company", "ticker": None},
            ]}, {}
        return {"summary": "Cramer likes Apple.", "key_topics": ["apple"], "segments": []}, {}


class FakeSentimentApi:
    def __init__(self):
        self.calls = 0

    def deliver(self, row, transcript):
        self.calls += 1
        return DeliveryStatus.sent, "sentiment:cnbc:x"


class FakeWatchlistApi:
    def __init__(self):
        self.calls = 0

    def submit(self, row, transcript):
        self.calls += 1
        return WatchlistStatus.submitted


def _pipeline():
    return Pipeline(
        transcript_repo=FakeTranscriptRepo(), distillation_repo=FakeDistillationRepo(),
        sentiment_repo=FakeSentimentRepo(), entity_repo=FakeEntityRepo(), run_repo=FakeRunRepo(),
        archive_client=None, llm_client=FakeLLM(),
        sentiment_api=FakeSentimentApi(), watchlist_api=FakeWatchlistApi(),
        model="m1", distill_prompt_version="v1", sentiment_prompt_version="v1",
        entity_prompt_version="v1",
    )


class TestPipeline:
    def test_fetched_to_done(self):
        p = _pipeline()
        t = p.transcripts.add_fetched("CNBC_20260702_220000_Mad_Money", "raw transcript text")
        c = p.process_one(t)
        assert p.transcripts.get_by_id(t.id).status == TranscriptStatus.done
        assert c["distilled"] == 1
        assert c["sentiments_sent"] == 2          # AAPL + ALL
        assert c["entities_submitted"] == 1       # AAPL resolved; private co unresolved
        assert p.distillations.get_current(t.id).summary == "Cramer likes Apple."
        # unresolved entity was persisted but not submitted
        assert any(e.watchlist_status == WatchlistStatus.unresolved for e in p.entities.rows.values())

    def test_reprocess_reruns_and_counts(self):
        p = _pipeline()
        t = p.transcripts.add_fetched("CNBC_20260702_220000_Mad_Money", "raw text")
        p.process_one(t)
        c = p.reprocess(p.transcripts.get_by_id(t.id))
        assert c["reprocessed"] == 1
        assert p.transcripts.get_by_id(t.id).status == TranscriptStatus.done

    def test_restart_refetches_and_reruns(self):
        class FakeArchive:
            def fetch_page_transcript(self, identifier):
                return "fresh transcript text"

        p = _pipeline()
        p.archive = FakeArchive()
        t = p.transcripts.add_fetched("CNBC_20260702_220000_Mad_Money", "old text")
        p.process_one(t)
        c = p.restart(p.transcripts.get_by_id(t.id))
        assert c["reprocessed"] == 1
        refreshed = p.transcripts.get_by_id(t.id)
        assert refreshed.status == TranscriptStatus.done
        assert refreshed.raw_text == "fresh transcript text"


class TestWakeTiming:
    def test_seconds_until_wake_same_day(self):
        now = datetime(2026, 7, 4, 5, 0, 0)
        assert seconds_until_wake("06:00", now) == 3600

    def test_seconds_until_wake_next_day(self):
        now = datetime(2026, 7, 4, 7, 0, 0)
        assert seconds_until_wake("06:00", now) == 23 * 3600
