"""Distillation repository (versioned by model + prompt_version)."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.models.domain import Distillation

_COLUMNS = (
    "id, transcript_id, model, prompt_version, summary, key_topics, "
    "segments, token_usage, is_current, created_at"
)


def _row_to_distillation(row: dict) -> Distillation:
    return Distillation.model_validate(dict(row))


class DistillationRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def upsert(self, d: Distillation) -> Distillation:
        """Insert (or update in place) the distillation for a (transcript, model, prompt).

        The newest version becomes ``is_current``; prior versions are demoted.
        """
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE cnbc.distillations SET is_current = false "
                    "WHERE transcript_id = :tid"
                ),
                {"tid": d.transcript_id},
            )
            row = conn.execute(
                text(
                    """
                    INSERT INTO cnbc.distillations
                        (transcript_id, model, prompt_version, summary,
                         key_topics, segments, token_usage, is_current)
                    VALUES
                        (:transcript_id, :model, :prompt_version, :summary,
                         :key_topics::jsonb, :segments::jsonb, :token_usage::jsonb, true)
                    ON CONFLICT (transcript_id, model, prompt_version) DO UPDATE
                       SET summary = EXCLUDED.summary,
                           key_topics = EXCLUDED.key_topics,
                           segments = EXCLUDED.segments,
                           token_usage = EXCLUDED.token_usage,
                           is_current = true,
                           created_at = now()
                    RETURNING {cols}
                    """.format(cols=_COLUMNS)
                ),
                {
                    "transcript_id": d.transcript_id,
                    "model": d.model,
                    "prompt_version": d.prompt_version,
                    "summary": d.summary,
                    "key_topics": json.dumps(d.key_topics),
                    "segments": json.dumps(d.segments),
                    "token_usage": json.dumps(d.token_usage) if d.token_usage is not None else None,
                },
            ).mappings().first()
        return _row_to_distillation(row)

    def get_current(self, transcript_id: int) -> Distillation | None:
        sql = text(
            f"SELECT {_COLUMNS} FROM cnbc.distillations "
            "WHERE transcript_id = :tid AND is_current ORDER BY created_at DESC LIMIT 1"
        )
        with self.engine.connect() as conn:
            row = conn.execute(sql, {"tid": transcript_id}).mappings().first()
        return _row_to_distillation(row) if row else None
