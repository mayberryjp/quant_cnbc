"""Transcript repository: discovery, fetch state, and read/reprocess queries."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.models.domain import Transcript, TranscriptStatus

_COLUMNS = (
    "id, archive_identifier, show_id, show_slug, air_date, broadcast_start, "
    "title, source_url, caption_file, content_hash, status, attempts, last_error, "
    "archive_addeddate, discovered_at, fetched_at, distilled_at, delivered_at"
)

# Derived scalar metrics for read/API queries. Computed in SQL so that the long
# raw_text / summary bodies never leave the database, only their lengths/counts.
_METRICS = (
    "char_length(t.raw_text) AS raw_char_count, "
    "d.summary_char_count, d.key_topic_count, d.segment_count, "
    "s.sentiment_count, e.entity_count"
)

_METRIC_JOINS = (
    " LEFT JOIN LATERAL ("
    "   SELECT char_length(summary) AS summary_char_count, "
    "          jsonb_array_length(key_topics) AS key_topic_count, "
    "          jsonb_array_length(segments) AS segment_count "
    "   FROM cnbc.distillations "
    "   WHERE transcript_id = t.id AND is_current "
    "   ORDER BY created_at DESC LIMIT 1"
    " ) d ON true"
    " LEFT JOIN LATERAL ("
    "   SELECT count(*) AS sentiment_count FROM cnbc.sentiments WHERE transcript_id = t.id"
    " ) s ON true"
    " LEFT JOIN LATERAL ("
    "   SELECT count(*) AS entity_count "
    "   FROM cnbc.referenced_entities WHERE transcript_id = t.id"
    " ) e ON true"
)


def _row_to_transcript(row: dict, *, raw_text: str | None = None) -> Transcript:
    data = dict(row)
    if raw_text is not None:
        data["raw_text"] = raw_text
    return Transcript.model_validate(data)


class TranscriptRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # -- discovery ---------------------------------------------------------
    def upsert_discovered(self, items: list[dict]) -> int:
        """Insert newly-discovered items; skip ones already known. Returns inserted count."""
        if not items:
            return 0
        sql = text(
            """
            INSERT INTO cnbc.transcripts
                (archive_identifier, show_slug, air_date, broadcast_start,
                 title, source_url, archive_addeddate, status)
            VALUES
                (:archive_identifier, :show_slug, :air_date, :broadcast_start,
                 :title, :source_url, :archive_addeddate, 'discovered')
            ON CONFLICT (archive_identifier) DO NOTHING
            """
        )
        inserted = 0
        with self.engine.begin() as conn:
            for it in items:
                result = conn.execute(sql, it)
                inserted += result.rowcount or 0
        return inserted

    # -- lookups -----------------------------------------------------------
    def get_by_identifier(self, archive_identifier: str) -> Transcript | None:
        sql = text(
            f"SELECT {_COLUMNS}, raw_text FROM cnbc.transcripts "
            "WHERE archive_identifier = :aid"
        )
        with self.engine.connect() as conn:
            row = conn.execute(sql, {"aid": archive_identifier}).mappings().first()
        return _row_to_transcript(row) if row else None

    def get_by_id(self, transcript_id: int) -> Transcript | None:
        sql = text(
            f"SELECT {_COLUMNS}, raw_text, {_METRICS} "
            f"FROM cnbc.transcripts t{_METRIC_JOINS} WHERE t.id = :id"
        )
        with self.engine.connect() as conn:
            row = conn.execute(sql, {"id": transcript_id}).mappings().first()
        return _row_to_transcript(row) if row else None

    # -- state transitions -------------------------------------------------
    def mark_fetched(
        self, transcript_id: int, *, raw_text: str, content_hash: str,
        caption_file: str | None = None,
    ) -> None:
        sql = text(
            """
            UPDATE cnbc.transcripts
               SET raw_text = :raw_text, content_hash = :content_hash,
                   caption_file = :caption_file, status = 'fetched',
                   fetched_at = :now, last_error = NULL
             WHERE id = :id
            """
        )
        with self.engine.begin() as conn:
            conn.execute(sql, {
                "id": transcript_id, "raw_text": raw_text,
                "content_hash": content_hash, "caption_file": caption_file,
                "now": datetime.now(timezone.utc),
            })

    def set_status(
        self, transcript_id: int, status: TranscriptStatus | str, *,
        last_error: str | None = None, bump_attempts: bool = False,
    ) -> None:
        status_val = status.value if isinstance(status, TranscriptStatus) else status
        attempts_expr = "attempts + 1" if bump_attempts else "attempts"
        sql = text(
            f"""
            UPDATE cnbc.transcripts
               SET status = :status, last_error = :err, attempts = {attempts_expr}
             WHERE id = :id
            """
        )
        with self.engine.begin() as conn:
            conn.execute(sql, {"id": transcript_id, "status": status_val, "err": last_error})

    def touch_stage(self, transcript_id: int, stage: str) -> None:
        """Set a per-stage completion timestamp (stage in distilled|delivered)."""
        column = {"distilled": "distilled_at", "delivered": "delivered_at"}[stage]
        sql = text(
            f"UPDATE cnbc.transcripts SET {column} = :now WHERE id = :id"
        )
        with self.engine.begin() as conn:
            conn.execute(sql, {"id": transcript_id, "now": datetime.now(timezone.utc)})

    def reset_for_reprocess(self, transcript_id: int) -> bool:
        """Reset an item to 'fetched' so passes re-run from the saved raw_text."""
        sql = text(
            """
            UPDATE cnbc.transcripts SET status = 'fetched', last_error = NULL
             WHERE id = :id AND raw_text IS NOT NULL
            """
        )
        with self.engine.begin() as conn:
            return (conn.execute(sql, {"id": transcript_id}).rowcount or 0) > 0

    def reset_full(self, transcript_id: int) -> bool:
        """Reset an item all the way back to 'discovered'.

        Clears the fetched transcript text and every downstream stage marker so
        the next run re-fetches from archive.org and re-runs all passes.
        Retry attempts are intentionally preserved so repeated failures can be
        tracked over time.
        """
        sql = text(
            """
            UPDATE cnbc.transcripts
               SET status = 'discovered', raw_text = NULL, content_hash = NULL,
                   caption_file = NULL, last_error = NULL,
                   fetched_at = NULL, distilled_at = NULL, delivered_at = NULL
             WHERE id = :id
            """
        )
        with self.engine.begin() as conn:
            return (conn.execute(sql, {"id": transcript_id}).rowcount or 0) > 0

    def delete(self, transcript_id: int) -> bool:
        """Hard-delete a transcript and every derived row.

        The child tables (distillations, sentiments, referenced_entities) carry
        a NOT NULL foreign key to transcripts with no ON DELETE CASCADE, so they
        are removed first inside the same transaction. Returns whether a
        transcript row actually existed.
        """
        with self.engine.begin() as conn:
            for table in (
                "cnbc.distillations",
                "cnbc.sentiments",
                "cnbc.referenced_entities",
            ):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE transcript_id = :id"),
                    {"id": transcript_id},
                )
            result = conn.execute(
                text("DELETE FROM cnbc.transcripts WHERE id = :id"),
                {"id": transcript_id},
            )
            return (result.rowcount or 0) > 0

    # -- selection ---------------------------------------------------------
    def list_actionable(
        self, *, limit: int = 100, max_attempts: int = 5, include_failed: bool = True
    ) -> list[Transcript]:
        failed_clause = (
            " OR (status = 'failed' AND attempts < :max_attempts)"
            if include_failed
            else ""
        )
        sql = text(
            f"""
            SELECT {_COLUMNS}, raw_text FROM cnbc.transcripts
             WHERE status IN ('discovered','fetched','distilled')
                {failed_clause}
             ORDER BY broadcast_start NULLS LAST, id
             LIMIT :limit
            """
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"max_attempts": max_attempts, "limit": limit}).mappings().all()
        return [_row_to_transcript(r) for r in rows]

    def list(
        self, *, status: str | None = None, show: str | None = None,
        from_date=None, to_date=None, page: int = 1, page_size: int = 25,
    ) -> tuple[list[Transcript], int]:
        clauses, params = [], {}
        if status:
            clauses.append("status = :status")
            params["status"] = status
        if show:
            clauses.append("show_slug = :show")
            params["show"] = show
        if from_date:
            clauses.append("air_date >= :from_date")
            params["from_date"] = from_date
        if to_date:
            clauses.append("air_date <= :to_date")
            params["to_date"] = to_date
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.engine.connect() as conn:
            total = conn.execute(
                text(f"SELECT count(*) FROM cnbc.transcripts{where}"), params
            ).scalar_one()
            params["limit"] = page_size
            params["offset"] = (page - 1) * page_size
            rows = conn.execute(
                text(
                    f"SELECT {_COLUMNS}, {_METRICS} "
                    f"FROM cnbc.transcripts t{_METRIC_JOINS}{where} "
                    "ORDER BY broadcast_start DESC NULLS LAST LIMIT :limit OFFSET :offset"
                ),
                params,
            ).mappings().all()
        return [_row_to_transcript(r) for r in rows], total

    def reprocess_candidates(
        self, *, show: str | None = None, from_date=None, to_date=None,
        only_stale: bool = False, current_model: str | None = None,
        current_prompt: str | None = None,
    ) -> list[Transcript]:
        clauses = ["t.raw_text IS NOT NULL"]
        params: dict = {}
        if show:
            clauses.append("t.show_slug = :show")
            params["show"] = show
        if from_date:
            clauses.append("t.air_date >= :from_date")
            params["from_date"] = from_date
        if to_date:
            clauses.append("t.air_date <= :to_date")
            params["to_date"] = to_date
        if only_stale:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM cnbc.distillations d "
                "WHERE d.transcript_id = t.id AND d.is_current "
                "AND d.model = :cur_model AND d.prompt_version = :cur_prompt)"
            )
            params["cur_model"] = current_model
            params["cur_prompt"] = current_prompt
        where = " WHERE " + " AND ".join(clauses)
        sql = text(f"SELECT {_COLUMNS} FROM cnbc.transcripts t{where} ORDER BY t.id")
        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        return [_row_to_transcript(r) for r in rows]

    def restart_candidates(
        self, *, show: str | None = None, from_date=None, to_date=None,
    ) -> list[Transcript]:
        """All transcripts matching the filters, regardless of status/raw_text.

        Used by a full restart, which re-fetches from archive.org, so items that
        never fetched (or failed) are included too.
        """
        clauses: list[str] = []
        params: dict = {}
        if show:
            clauses.append("show_slug = :show")
            params["show"] = show
        if from_date:
            clauses.append("air_date >= :from_date")
            params["from_date"] = from_date
        if to_date:
            clauses.append("air_date <= :to_date")
            params["to_date"] = to_date
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = text(f"SELECT {_COLUMNS} FROM cnbc.transcripts{where} ORDER BY id")
        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        return [_row_to_transcript(r) for r in rows]

    def failed_candidates(
        self, *, show: str | None = None, from_date=None, to_date=None,
        max_attempts: int | None = None,
    ) -> list[Transcript]:
        """Transcripts currently in the 'failed' state, for a retry sweep.

        Optionally filtered by show/date and capped to rows whose attempt count
        is still below ``max_attempts`` (so exhausted items are left alone).
        """
        clauses: list[str] = ["status = 'failed'"]
        params: dict = {}
        if show:
            clauses.append("show_slug = :show")
            params["show"] = show
        if from_date:
            clauses.append("air_date >= :from_date")
            params["from_date"] = from_date
        if to_date:
            clauses.append("air_date <= :to_date")
            params["to_date"] = to_date
        if max_attempts is not None:
            clauses.append("attempts < :max_attempts")
            params["max_attempts"] = max_attempts
        where = " WHERE " + " AND ".join(clauses)
        sql = text(f"SELECT {_COLUMNS} FROM cnbc.transcripts{where} ORDER BY id")
        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        return [_row_to_transcript(r) for r in rows]

