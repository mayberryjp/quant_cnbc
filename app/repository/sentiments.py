"""Sentiment repository (LLM pass 2 output + delivery state)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.models.domain import DeliveryStatus, Sentiment

_COLUMNS = (
    "id, transcript_id, subject_type, subject, sentiment_label, sentiment_score, "
    "confidence, horizon, reason, model, prompt_version, idempotency_key, "
    "delivery_status, sentiment_id, delivered_at, created_at"
)


def _row(r: dict) -> Sentiment:
    return Sentiment.model_validate(dict(r))


class SentimentRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def insert(self, s: Sentiment) -> int:
        """Insert a sentiment row; idempotent on idempotency_key. Returns the row id."""
        params = {
            "transcript_id": s.transcript_id,
            "subject_type": s.subject_type.value,
            "subject": s.subject,
            "sentiment_label": s.sentiment_label.value,
            "sentiment_score": s.sentiment_score,
            "confidence": s.confidence,
            "horizon": s.horizon,
            "reason": s.reason,
            "model": s.model,
            "prompt_version": s.prompt_version,
            "idempotency_key": s.idempotency_key,
        }
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO cnbc.sentiments
                        (transcript_id, subject_type, subject, sentiment_label,
                         sentiment_score, confidence, horizon, reason, model,
                         prompt_version, idempotency_key)
                    VALUES
                        (:transcript_id, :subject_type, :subject, :sentiment_label,
                         :sentiment_score, :confidence, :horizon, :reason, :model,
                         :prompt_version, :idempotency_key)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
                    """
                ),
                params,
            ).first()
            if row is not None:
                return int(row[0])
            existing = conn.execute(
                text("SELECT id FROM cnbc.sentiments WHERE idempotency_key = :k"),
                {"k": s.idempotency_key},
            ).first()
        return int(existing[0])

    def set_delivery(
        self, sentiment_row_id: int, status: DeliveryStatus | str, *,
        sentiment_id: str | None = None, delivered_at: datetime | None = None,
    ) -> None:
        status_val = status.value if isinstance(status, DeliveryStatus) else status
        sql = text(
            """
            UPDATE cnbc.sentiments
               SET delivery_status = :status, sentiment_id = :sid, delivered_at = :dat
             WHERE id = :id
            """
        )
        with self.engine.begin() as conn:
            conn.execute(sql, {
                "id": sentiment_row_id, "status": status_val,
                "sid": sentiment_id, "dat": delivered_at,
            })

    def list_undelivered(self, limit: int = 200) -> list[Sentiment]:
        sql = text(
            f"SELECT {_COLUMNS} FROM cnbc.sentiments "
            "WHERE delivery_status IN ('pending','failed') ORDER BY id LIMIT :limit"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"limit": limit}).mappings().all()
        return [_row(r) for r in rows]

    def list(
        self, *, subject: str | None = None, status: str | None = None,
        page: int = 1, page_size: int = 25,
    ) -> tuple[list[Sentiment], int]:
        clauses, params = [], {}
        if subject:
            clauses.append("subject = :subject")
            params["subject"] = subject
        if status:
            clauses.append("delivery_status = :status")
            params["status"] = status
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.engine.connect() as conn:
            total = conn.execute(
                text(f"SELECT count(*) FROM cnbc.sentiments{where}"), params
            ).scalar_one()
            params["limit"] = page_size
            params["offset"] = (page - 1) * page_size
            rows = conn.execute(
                text(f"SELECT {_COLUMNS} FROM cnbc.sentiments{where} "
                     "ORDER BY id DESC LIMIT :limit OFFSET :offset"),
                params,
            ).mappings().all()
        return [_row(r) for r in rows], total
