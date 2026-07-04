"""Referenced-entity repository (LLM pass 3 output + watchlist state)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.models.domain import ReferencedEntity, WatchlistStatus

_COLUMNS = (
    "id, transcript_id, raw_mention, entity_type, company_name, ticker, speaker, "
    "direction, confidence, context, model, prompt_version, idempotency_key, "
    "watchlist_status, submitted_at, created_at"
)


def _row(r: dict) -> ReferencedEntity:
    return ReferencedEntity.model_validate(dict(r))


class EntityRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def insert(self, e: ReferencedEntity) -> int:
        """Insert an entity row; idempotent on idempotency_key. Returns the row id."""
        params = {
            "transcript_id": e.transcript_id,
            "raw_mention": e.raw_mention,
            "entity_type": e.entity_type.value,
            "company_name": e.company_name,
            "ticker": e.ticker,
            "speaker": e.speaker,
            "direction": e.direction.value if e.direction else None,
            "confidence": e.confidence,
            "context": e.context,
            "model": e.model,
            "prompt_version": e.prompt_version,
            "idempotency_key": e.idempotency_key,
            "watchlist_status": e.watchlist_status.value,
        }
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO cnbc.referenced_entities
                        (transcript_id, raw_mention, entity_type, company_name,
                         ticker, speaker, direction, confidence, context, model,
                         prompt_version, idempotency_key, watchlist_status)
                    VALUES
                        (:transcript_id, :raw_mention, :entity_type, :company_name,
                         :ticker, :speaker, :direction, :confidence, :context, :model,
                         :prompt_version, :idempotency_key, :watchlist_status)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
                    """
                ),
                params,
            ).first()
            if row is not None:
                return int(row[0])
            existing = conn.execute(
                text("SELECT id FROM cnbc.referenced_entities WHERE idempotency_key = :k"),
                {"k": e.idempotency_key},
            ).first()
        return int(existing[0])

    def set_watchlist(
        self, entity_row_id: int, status: WatchlistStatus | str, *,
        submitted_at: datetime | None = None,
    ) -> None:
        status_val = status.value if isinstance(status, WatchlistStatus) else status
        sql = text(
            "UPDATE cnbc.referenced_entities "
            "SET watchlist_status = :status, submitted_at = :sat WHERE id = :id"
        )
        with self.engine.begin() as conn:
            conn.execute(sql, {"id": entity_row_id, "status": status_val, "sat": submitted_at})

    def list_pending_submission(self, limit: int = 200) -> list[ReferencedEntity]:
        sql = text(
            f"SELECT {_COLUMNS} FROM cnbc.referenced_entities "
            "WHERE watchlist_status IN ('pending','failed') AND ticker IS NOT NULL "
            "ORDER BY id LIMIT :limit"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"limit": limit}).mappings().all()
        return [_row(r) for r in rows]

    def list(
        self, *, ticker: str | None = None, status: str | None = None,
        page: int = 1, page_size: int = 25,
    ) -> tuple[list[ReferencedEntity], int]:
        clauses, params = [], {}
        if ticker:
            clauses.append("ticker = :ticker")
            params["ticker"] = ticker.upper()
        if status:
            clauses.append("watchlist_status = :status")
            params["status"] = status
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.engine.connect() as conn:
            total = conn.execute(
                text(f"SELECT count(*) FROM cnbc.referenced_entities{where}"), params
            ).scalar_one()
            params["limit"] = page_size
            params["offset"] = (page - 1) * page_size
            rows = conn.execute(
                text(f"SELECT {_COLUMNS} FROM cnbc.referenced_entities{where} "
                     "ORDER BY id DESC LIMIT :limit OFFSET :offset"),
                params,
            ).mappings().all()
        return [_row(r) for r in rows], total
