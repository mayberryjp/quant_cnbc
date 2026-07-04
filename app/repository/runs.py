"""Ingest run log, heartbeat, discovery cursor, and stats."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.models.domain import IngestCursor, RunStatus

_COUNTERS = (
    "shows_processed", "transcripts_fetched", "distilled",
    "reprocessed", "sentiments_sent", "entities_submitted", "failures",
)


class RunRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # -- run lifecycle -----------------------------------------------------
    def start_run(self, run_date: date) -> None:
        sql = text(
            """
            INSERT INTO cnbc.ingest_runs (run_date, started_at, status, heartbeat_at)
            VALUES (:run_date, :now, 'running', :now)
            ON CONFLICT (run_date) DO UPDATE
               SET started_at = COALESCE(cnbc.ingest_runs.started_at, EXCLUDED.started_at),
                   status = 'running', heartbeat_at = EXCLUDED.heartbeat_at
            """
        )
        with self.engine.begin() as conn:
            conn.execute(sql, {"run_date": run_date, "now": datetime.now(timezone.utc)})

    def add_counters(self, run_date: date, **counters: int) -> None:
        sets = []
        params: dict = {"run_date": run_date, "now": datetime.now(timezone.utc)}
        for name, value in counters.items():
            if name not in _COUNTERS or not value:
                continue
            sets.append(f"{name} = cnbc.ingest_runs.{name} + :{name}")
            params[name] = value
        if not sets:
            self.heartbeat(run_date)
            return
        sql = text(
            f"UPDATE cnbc.ingest_runs SET {', '.join(sets)}, heartbeat_at = :now "
            "WHERE run_date = :run_date"
        )
        with self.engine.begin() as conn:
            conn.execute(sql, params)

    def heartbeat(self, run_date: date) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE cnbc.ingest_runs SET heartbeat_at = :now WHERE run_date = :run_date"),
                {"run_date": run_date, "now": datetime.now(timezone.utc)},
            )

    def complete_run(self, run_date: date, status: RunStatus | str) -> None:
        status_val = status.value if isinstance(status, RunStatus) else status
        sql = text(
            "UPDATE cnbc.ingest_runs SET status = :status, completed_at = :now, "
            "heartbeat_at = :now WHERE run_date = :run_date"
        )
        with self.engine.begin() as conn:
            conn.execute(sql, {"run_date": run_date, "status": status_val, "now": datetime.now(timezone.utc)})

    def latest_run(self) -> dict | None:
        sql = text(
            "SELECT run_date, status, heartbeat_at, completed_at "
            "FROM cnbc.ingest_runs ORDER BY run_date DESC LIMIT 1"
        )
        with self.engine.connect() as conn:
            row = conn.execute(sql).mappings().first()
        return dict(row) if row else None

    # -- discovery cursor --------------------------------------------------
    def get_cursor(self, collection: str) -> IngestCursor | None:
        sql = text(
            "SELECT collection, last_addeddate, last_identifier, updated_at "
            "FROM cnbc.ingest_cursor WHERE collection = :c"
        )
        with self.engine.connect() as conn:
            row = conn.execute(sql, {"c": collection}).mappings().first()
        return IngestCursor.model_validate(dict(row)) if row else None

    def set_cursor(
        self, collection: str, *, last_addeddate: datetime | None,
        last_identifier: str | None,
    ) -> None:
        sql = text(
            """
            INSERT INTO cnbc.ingest_cursor (collection, last_addeddate, last_identifier, updated_at)
            VALUES (:c, :lad, :lid, :now)
            ON CONFLICT (collection) DO UPDATE
               SET last_addeddate = EXCLUDED.last_addeddate,
                   last_identifier = EXCLUDED.last_identifier,
                   updated_at = EXCLUDED.updated_at
            """
        )
        with self.engine.begin() as conn:
            conn.execute(sql, {
                "c": collection, "lad": last_addeddate,
                "lid": last_identifier, "now": datetime.now(timezone.utc),
            })

    # -- stats -------------------------------------------------------------
    def stats(self) -> dict:
        with self.engine.connect() as conn:
            agg = conn.execute(
                text(
                    "SELECT COALESCE(sum(transcripts_fetched),0) AS transcripts_fetched, "
                    "COALESCE(sum(distilled),0) AS distilled, "
                    "COALESCE(sum(reprocessed),0) AS reprocessed, "
                    "COALESCE(sum(sentiments_sent),0) AS sentiments_sent, "
                    "COALESCE(sum(entities_submitted),0) AS entities_submitted, "
                    "COALESCE(sum(failures),0) AS failures FROM cnbc.ingest_runs"
                )
            ).mappings().first()
            total = conn.execute(text("SELECT count(*) FROM cnbc.transcripts")).scalar_one()
            latest = self.latest_run()
        out = dict(agg) if agg else {}
        out["transcripts_total"] = int(total)
        if latest:
            out["last_run_date"] = str(latest.get("run_date"))
            out["last_run_status"] = latest.get("status")
            hb = latest.get("heartbeat_at")
            out["last_heartbeat"] = str(hb) if hb else None
        return out
