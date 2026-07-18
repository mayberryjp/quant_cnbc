"""Slice 7 + 9 tests: read API, readiness, reprocess/trigger endpoints (mocked deps)."""

from __future__ import annotations

import time
from collections import Counter
from datetime import date

from app.models.domain import (
    ReferencedEntity,
    Sentiment,
    Transcript,
    TranscriptStatus,
    WatchlistStatus,
)


def _transcript(tid=1, status=TranscriptStatus.done):
    return Transcript(
        id=tid, archive_identifier="CNBC_20260702_220000_Mad_Money", show_slug="Mad_Money",
        air_date=date(2026, 7, 2), source_url="https://archive.org/details/x", status=status,
    )


class FakeTranscriptRepo:
    def list(self, **kw):
        return [_transcript()], 1

    def get_by_id(self, tid):
        return _transcript(tid) if tid == 1 else None

    def get_by_identifier(self, aid):
        return _transcript() if aid == "CNBC_20260702_220000_Mad_Money" else None

    def delete(self, tid):
        self.deleted = getattr(self, "deleted", [])
        if tid in (1, 2):
            self.deleted.append(tid)
            return True
        return False


class TestReadApi:
    def test_list_transcripts(self, app_client, monkeypatch):
        from app.models.domain import Distillation

        class DRepo:
            def get_current_map(self, tids):
                return {
                    tid: Distillation(
                        id=1, transcript_id=tid, model="m1", prompt_version="v1",
                        summary="Short distilled summary.", key_topics=[], segments=[],
                    )
                    for tid in tids
                }

        monkeypatch.setattr("app.dependencies.transcript_repo", lambda *a, **k: FakeTranscriptRepo())
        monkeypatch.setattr("app.dependencies.distillation_repo", lambda *a, **k: DRepo())
        resp = app_client.get("/transcripts?status=done")
        assert resp.status_int == 200
        assert resp.json["total"] == 1
        assert resp.json["items"][0]["archive_identifier"] == "CNBC_20260702_220000_Mad_Money"
        assert resp.json["items"][0]["summary"] == "Short distilled summary."

    def test_get_transcript_404(self, app_client, monkeypatch):
        monkeypatch.setattr("app.dependencies.transcript_repo", lambda *a, **k: FakeTranscriptRepo())
        resp = app_client.get("/transcripts/999", expect_errors=True)
        assert resp.status_int == 404

    def test_get_transcript_detail_returns_raw_text_and_summary(self, app_client, monkeypatch):
        from app.models.domain import Distillation

        class TRepo(FakeTranscriptRepo):
            def get_by_id(self, tid):
                t = _transcript(tid)
                t.raw_text = ">> Full original transcript text."
                return t

        class DRepo:
            def get_current(self, tid):
                return Distillation(
                    id=1, transcript_id=tid, model="m1", prompt_version="v1",
                    summary="Short distilled summary.", key_topics=["apple"], segments=[],
                    token_usage={"prompt_chars": 41, "completion_chars": 24},
                )

        monkeypatch.setattr("app.dependencies.transcript_repo", lambda *a, **k: TRepo())
        monkeypatch.setattr("app.dependencies.distillation_repo", lambda *a, **k: DRepo())
        resp = app_client.get("/transcripts/1")
        assert resp.status_int == 200
        assert resp.json["raw_text"] == ">> Full original transcript text."
        assert resp.json["distillation"]["summary"] == "Short distilled summary."

    def test_list_entities(self, app_client, monkeypatch):
        class R:
            def list(self, **kw):
                return [ReferencedEntity(
                    id=1, transcript_id=1, raw_mention="Apple", entity_type="company",
                    ticker="AAPL", model="m1", prompt_version="v1",
                    idempotency_key="k", watchlist_status=WatchlistStatus.submitted,
                )], 1
        monkeypatch.setattr("app.dependencies.entity_repo", lambda *a, **k: R())
        resp = app_client.get("/entities?ticker=AAPL")
        assert resp.status_int == 200
        assert resp.json["items"][0]["ticker"] == "AAPL"

    def test_list_sentiments(self, app_client, monkeypatch):
        class R:
            def list(self, **kw):
                return [Sentiment(
                    id=1, transcript_id=1, subject="AAPL", sentiment_label="bullish",
                    model="m1", prompt_version="v1", idempotency_key="k",
                )], 1
        monkeypatch.setattr("app.dependencies.sentiment_repo", lambda *a, **k: R())
        resp = app_client.get("/sentiments?subject=AAPL")
        assert resp.status_int == 200
        assert resp.json["items"][0]["subject"] == "AAPL"


class TestDeleteEndpoint:
    def test_delete_transcript(self, app_client, monkeypatch):
        fake = FakeTranscriptRepo()
        monkeypatch.setattr("app.dependencies.transcript_repo", lambda *a, **k: fake)
        resp = app_client.delete("/transcripts/1")
        assert resp.status_int == 200
        assert resp.json == {"status": "deleted", "id": 1}
        assert fake.deleted == [1]

    def test_delete_transcript_404(self, app_client, monkeypatch):
        monkeypatch.setattr("app.dependencies.transcript_repo", lambda *a, **k: FakeTranscriptRepo())
        resp = app_client.delete("/transcripts/999", expect_errors=True)
        assert resp.status_int == 404


class TestReadiness:
    def test_ready_reports_not_ready_when_db_down(self, app_client, monkeypatch):
        monkeypatch.setattr("app.db.ping", lambda: False)
        resp = app_client.get("/cnbc/ready")
        assert resp.status_int == 200
        assert resp.json["status"] == "not_ready"
        assert resp.json["database"] == "unavailable"


class FakePipeline:
    def __init__(self):
        self.transcripts = FakeTranscriptRepo()
        self.reprocessed = []

    def reprocess(self, t):
        self.reprocessed.append(t.archive_identifier)
        return Counter({"reprocessed": 1})

    def restart(self, t):
        self.restarted = getattr(self, "restarted", [])
        self.restarted.append(t.archive_identifier)
        return Counter({"reprocessed": 1})

    def run(self, run_date):
        return Counter({"transcripts_fetched": 3})

    def retry_failed(self, **kw):
        self.retry_kwargs = kw
        return Counter({"retried": 2, "reprocessed": 2, "distilled": 1, "failures": 1})


class TestReprocessEndpoints:
    @staticmethod
    def _await_job(app_client, resp):
        """Block until the background job referenced by ``resp`` finishes."""
        job_id = resp.json["job_id"]
        for _ in range(200):
            job = app_client.get(f"/jobs/{job_id}").json
            if job["status"] != "running":
                return job
            time.sleep(0.01)
        raise AssertionError("job did not finish in time")

    def test_reprocess_one(self, app_client, monkeypatch):
        fake = FakePipeline()
        monkeypatch.setattr("app.services.ingest_worker.build_pipeline", lambda *a, **k: fake)
        resp = app_client.post("/transcripts/CNBC_20260702_220000_Mad_Money/reprocess")
        assert resp.status_int == 202
        assert resp.json["status"] == "accepted"
        job = self._await_job(app_client, resp)
        assert job["status"] == "completed"
        assert job["result"]["reprocessed"] == 1
        assert fake.reprocessed == ["CNBC_20260702_220000_Mad_Money"]

    def test_reprocess_one_404(self, app_client, monkeypatch):
        monkeypatch.setattr("app.services.ingest_worker.build_pipeline", lambda *a, **k: FakePipeline())
        resp = app_client.post("/transcripts/UNKNOWN/reprocess", expect_errors=True)
        assert resp.status_int == 404

    def test_restart_one(self, app_client, monkeypatch):
        fake = FakePipeline()
        monkeypatch.setattr("app.services.ingest_worker.build_pipeline", lambda *a, **k: fake)
        resp = app_client.post("/transcripts/CNBC_20260702_220000_Mad_Money/restart")
        assert resp.status_int == 202
        assert resp.json["status"] == "accepted"
        job = self._await_job(app_client, resp)
        assert job["status"] == "completed"
        assert job["result"]["reprocessed"] == 1
        assert fake.restarted == ["CNBC_20260702_220000_Mad_Money"]

    def test_restart_one_404(self, app_client, monkeypatch):
        monkeypatch.setattr("app.services.ingest_worker.build_pipeline", lambda *a, **k: FakePipeline())
        resp = app_client.post("/transcripts/UNKNOWN/restart", expect_errors=True)
        assert resp.status_int == 404

    def test_trigger_run(self, app_client, monkeypatch):
        monkeypatch.setattr("app.services.ingest_worker.build_pipeline", lambda *a, **k: FakePipeline())
        resp = app_client.post_json("/runs/trigger", {"date": "2026-07-03"})
        assert resp.status_int == 202
        assert resp.json["status"] == "accepted"
        job = self._await_job(app_client, resp)
        assert job["result"]["transcripts_fetched"] == 3

    def test_retry_failed(self, app_client, monkeypatch):
        fake = FakePipeline()
        monkeypatch.setattr("app.services.ingest_worker.build_pipeline", lambda *a, **k: fake)
        resp = app_client.post_json("/retry-failed", {"show": "Mad_Money", "max_attempts": 5})
        assert resp.status_int == 202
        assert resp.json["status"] == "accepted"
        job = self._await_job(app_client, resp)
        assert job["result"]["retried"] == 2
        assert fake.retry_kwargs == {
            "show": "Mad_Money", "from_date": None, "to_date": None, "max_attempts": 5,
        }

    def test_retry_failed_empty_body(self, app_client, monkeypatch):
        fake = FakePipeline()
        monkeypatch.setattr("app.services.ingest_worker.build_pipeline", lambda *a, **k: fake)
        resp = app_client.post_json("/retry-failed", {})
        assert resp.status_int == 202
        self._await_job(app_client, resp)
        assert fake.retry_kwargs == {
            "show": None, "from_date": None, "to_date": None, "max_attempts": None,
        }

    def test_retry_failed_with_delete_after_attempts(self, app_client, monkeypatch):
        fake = FakePipeline()
        monkeypatch.setattr("app.services.ingest_worker.build_pipeline", lambda *a, **k: fake)
        resp = app_client.post_json("/retry-failed", {"delete_after_attempts": 10})
        assert resp.status_int == 202
        self._await_job(app_client, resp)
        assert fake.retry_kwargs == {
            "show": None,
            "from_date": None,
            "to_date": None,
            "max_attempts": None,
            "delete_after_attempts": 10,
        }
