"""Pipeline orchestration: drive transcripts through the processing state machine.

Fully dependency-injected so it can be unit-tested with in-memory fakes.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timezone

from app.models.domain import (
    Distillation,
    DeliveryStatus,
    Transcript,
    TranscriptStatus,
    WatchlistStatus,
)
from app.services import distiller, entity_pass, sentiment_pass
from app.services.transcript_fetcher import discover_new_items, fetch_transcript

log = logging.getLogger("quant_cnbc.pipeline")


class Pipeline:
    def __init__(
        self, *, transcript_repo, distillation_repo, sentiment_repo, entity_repo, run_repo,
        archive_client, llm_client, sentiment_api, watchlist_api,
        model: str, distill_prompt_version: str, sentiment_prompt_version: str,
        entity_prompt_version: str, collection: str = "TV-CNBC", lookback_days: int = 1,
        overlap_hours: int = 24, allowlist: list[str] | None = None, max_attempts: int = 5,
        distill_max_chunk_chars: int = 6000,
    ) -> None:
        self.transcripts = transcript_repo
        self.distillations = distillation_repo
        self.sentiments = sentiment_repo
        self.entities = entity_repo
        self.runs = run_repo
        self.archive = archive_client
        self.llm = llm_client
        self.sentiment_api = sentiment_api
        self.watchlist_api = watchlist_api
        self.model = model
        self.distill_pv = distill_prompt_version
        self.sentiment_pv = sentiment_prompt_version
        self.entity_pv = entity_prompt_version
        self.collection = collection
        self.lookback_days = lookback_days
        self.overlap_hours = overlap_hours
        self.allowlist = allowlist or []
        self.max_attempts = max_attempts
        self.distill_max_chunk_chars = distill_max_chunk_chars

    # -- discovery ---------------------------------------------------------
    def discover(self) -> int:
        return discover_new_items(
            self.archive, self.transcripts, self.runs,
            collection=self.collection, lookback_days=self.lookback_days,
            overlap_hours=self.overlap_hours, allowlist=self.allowlist,
        )

    # -- per-item processing ----------------------------------------------
    def process_one(self, transcript: Transcript) -> Counter:
        c: Counter = Counter()
        aid = transcript.archive_identifier
        try:
            status = transcript.status
            log.info("process %s: entering at status=%s", aid, getattr(status, "value", status))

            if status == TranscriptStatus.discovered:
                if not fetch_transcript(self.archive, self.transcripts, transcript):
                    log.warning("process %s: fetch failed, stopping", aid)
                    c["failures"] += 1
                    return c
                transcript = self.transcripts.get_by_id(transcript.id)
                status = TranscriptStatus.fetched
                c["transcripts_fetched"] += 1

            if status == TranscriptStatus.fetched:
                raw = transcript.raw_text or ""
                log.info("process %s: distilling (%d chars of transcript)", aid, len(raw))
                out, usage = distiller.distill(
                    self.llm, raw, max_chunk_chars=self.distill_max_chunk_chars
                )
                self.distillations.upsert(Distillation(
                    transcript_id=transcript.id, model=self.model,
                    prompt_version=self.distill_pv, summary=out.summary,
                    key_topics=out.key_topics,
                    segments=[s.model_dump() for s in out.segments],
                    token_usage=usage or None,
                ))
                self.transcripts.set_status(transcript.id, TranscriptStatus.distilled)
                self.transcripts.touch_stage(transcript.id, "distilled")
                status = TranscriptStatus.distilled
                c["distilled"] += 1
                log.info(
                    "process %s: distilled (summary=%d chars, %d topics, %d segments, tokens=%s)",
                    aid, len(out.summary), len(out.key_topics), len(out.segments),
                    (usage or {}).get("total_tokens", "?"),
                )

            if status == TranscriptStatus.distilled:
                current = self.distillations.get_current(transcript.id)
                summary = current.summary if current else (transcript.raw_text or "")
                log.info("process %s: delivering sentiment", aid)
                c.update(self._deliver_sentiment(transcript, summary))
                log.info("process %s: delivering entities", aid)
                c.update(self._deliver_entities(transcript, summary))
                self.transcripts.touch_stage(transcript.id, "delivered")
                self.transcripts.set_status(transcript.id, TranscriptStatus.done)
                log.info(
                    "process %s: DONE (sentiments_sent=%d, entities_submitted=%d)",
                    aid, c.get("sentiments_sent", 0), c.get("entities_submitted", 0),
                )
            return c
        except Exception as exc:  # isolate per-item failures
            log.exception("processing failed for %s", aid)
            self.transcripts.set_status(
                transcript.id, TranscriptStatus.failed,
                last_error=str(exc)[:500], bump_attempts=True,
            )
            c["failures"] += 1
            return c

    def _deliver_sentiment(self, transcript: Transcript, summary: str) -> Counter:
        c: Counter = Counter()
        out, _ = sentiment_pass.extract_sentiment(self.llm, summary)
        for row in sentiment_pass.build_rows(
            transcript, out, model=self.model, prompt_version=self.sentiment_pv
        ):
            row_id = self.sentiments.insert(row)
            status, sid = self.sentiment_api.deliver(row, transcript)
            delivered_at = datetime.now(timezone.utc) if status in (
                DeliveryStatus.sent, DeliveryStatus.duplicate
            ) else None
            self.sentiments.set_delivery(row_id, status, sentiment_id=sid, delivered_at=delivered_at)
            if status in (DeliveryStatus.sent, DeliveryStatus.duplicate):
                c["sentiments_sent"] += 1
        return c

    def _deliver_entities(self, transcript: Transcript, summary: str) -> Counter:
        c: Counter = Counter()
        out, _ = entity_pass.extract_entities(self.llm, summary)
        for row in entity_pass.build_rows(
            transcript, out, model=self.model, prompt_version=self.entity_pv
        ):
            row_id = self.entities.insert(row)
            if row.ticker:
                status = self.watchlist_api.submit(row, transcript)
                submitted_at = datetime.now(timezone.utc) if status in (
                    WatchlistStatus.submitted, WatchlistStatus.duplicate
                ) else None
                self.entities.set_watchlist(row_id, status, submitted_at=submitted_at)
                if status in (WatchlistStatus.submitted, WatchlistStatus.duplicate):
                    c["entities_submitted"] += 1
            else:
                self.entities.set_watchlist(row_id, WatchlistStatus.unresolved)
        return c

    # -- run ---------------------------------------------------------------
    def run(self, run_date: date | None = None, *, limit: int = 200) -> Counter:
        run_date = run_date or datetime.now(timezone.utc).date()
        log.info("run start: run_date=%s", run_date)
        self.runs.start_run(run_date)
        totals: Counter = Counter()
        try:
            self.discover()
            actionable = self.transcripts.list_actionable(limit=limit, max_attempts=self.max_attempts)
            log.info("run: %d actionable item(s) to process", len(actionable))
            for i, t in enumerate(actionable, 1):
                log.info("run: [%d/%d] %s", i, len(actionable), t.archive_identifier)
                c = self.process_one(t)
                totals.update(c)
                self.runs.add_counters(run_date, **c)
            status = "partial" if totals.get("failures") else "success"
        except Exception:
            log.exception("run failed")
            status = "failed"
        self.runs.complete_run(run_date, status)
        log.info("run complete: run_date=%s status=%s totals=%s", run_date, status, dict(totals))
        return totals

    # -- reprocessing (slice 9) -------------------------------------------
    def reprocess(self, transcript: Transcript, run_date: date | None = None) -> Counter:
        """Recalculate a saved transcript from 'fetched' through the passes."""
        self.transcripts.reset_for_reprocess(transcript.id)
        refreshed = self.transcripts.get_by_id(transcript.id)
        c = self.process_one(refreshed)
        c["reprocessed"] += 1
        if run_date is not None:
            self.runs.add_counters(run_date, **c)
        return c

    def restart(self, transcript: Transcript, run_date: date | None = None) -> Counter:
        """Fully restart an item: reset to 'discovered', re-fetch, re-run all passes."""
        self.transcripts.reset_full(transcript.id)
        refreshed = self.transcripts.get_by_id(transcript.id)
        c = self.process_one(refreshed)
        c["reprocessed"] += 1
        if run_date is not None:
            self.runs.add_counters(run_date, **c)
        return c

    def retry_failed(
        self, *, show: str | None = None, from_date=None, to_date=None,
        max_attempts: int | None = None, run_date: date | None = None,
    ) -> Counter:
        """Re-run every transcript stuck in the 'failed' state.

        Each item is fully restarted (reset to 'discovered' → re-fetch →
        re-run all passes), which recovers both fetch failures and downstream
        pass failures. Returns aggregate counters including ``retried`` (the
        number of failed items that were attempted).
        """
        candidates = self.transcripts.failed_candidates(
            show=show, from_date=from_date, to_date=to_date, max_attempts=max_attempts,
        )
        log.info("retry-failed: %d failed transcript(s) to retry", len(candidates))
        totals: Counter = Counter()
        for i, t in enumerate(candidates, 1):
            log.info("retry-failed: [%d/%d] %s", i, len(candidates), t.archive_identifier)
            totals.update(self.restart(t, run_date=run_date))
        totals["retried"] = len(candidates)
        log.info("retry-failed: retried %d, totals=%s", len(candidates), dict(totals))
        return totals

