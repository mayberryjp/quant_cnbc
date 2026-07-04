"""0001 - cnbc schema.

Revision ID: 0001_cnbc_schema
Create Date: 2026-07-04
"""

from alembic import op

revision = "0001_cnbc_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS cnbc")

    # -- catalog of tracked CNBC shows --
    op.execute(
        """
        CREATE TABLE cnbc.shows (
            id            SERIAL PRIMARY KEY,
            slug          TEXT NOT NULL UNIQUE,
            display_name  TEXT NOT NULL,
            archive_query TEXT,
            enabled       BOOLEAN NOT NULL DEFAULT true,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # -- raw retrieved transcripts (per archive.org item) --
    op.execute(
        """
        CREATE TABLE cnbc.transcripts (
            id                SERIAL PRIMARY KEY,
            archive_identifier TEXT NOT NULL UNIQUE,
            show_id           INTEGER REFERENCES cnbc.shows(id),
            show_slug         TEXT,
            air_date          DATE NOT NULL,
            broadcast_start   TIMESTAMPTZ,
            title             TEXT,
            source_url        TEXT NOT NULL,
            caption_file      TEXT,
            content_hash      TEXT UNIQUE,
            raw_text          TEXT,
            status            TEXT NOT NULL DEFAULT 'discovered',
            attempts          INTEGER NOT NULL DEFAULT 0,
            last_error        TEXT,
            archive_addeddate TIMESTAMPTZ,
            discovered_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            fetched_at        TIMESTAMPTZ,
            distilled_at      TIMESTAMPTZ,
            delivered_at      TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX ix_transcripts_status ON cnbc.transcripts (status)")
    op.execute("CREATE INDEX ix_transcripts_air_date ON cnbc.transcripts (air_date)")
    op.execute("CREATE INDEX ix_transcripts_show ON cnbc.transcripts (show_id)")
    op.execute(
        "CREATE INDEX ix_transcripts_addeddate ON cnbc.transcripts (archive_addeddate)"
    )

    # -- LLM distillations (versioned by model + prompt_version) --
    op.execute(
        """
        CREATE TABLE cnbc.distillations (
            id             SERIAL PRIMARY KEY,
            transcript_id  INTEGER NOT NULL REFERENCES cnbc.transcripts(id),
            model          TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            summary        TEXT NOT NULL,
            key_topics     JSONB NOT NULL DEFAULT '[]',
            segments       JSONB NOT NULL DEFAULT '[]',
            token_usage    JSONB,
            is_current     BOOLEAN NOT NULL DEFAULT true,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (transcript_id, model, prompt_version)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_distillations_transcript ON cnbc.distillations (transcript_id)"
    )

    # -- structured sentiment (LLM pass 2) --
    op.execute(
        """
        CREATE TABLE cnbc.sentiments (
            id              SERIAL PRIMARY KEY,
            transcript_id   INTEGER NOT NULL REFERENCES cnbc.transcripts(id),
            subject_type    TEXT NOT NULL DEFAULT 'ticker',
            subject         TEXT NOT NULL,
            sentiment_label TEXT NOT NULL,
            sentiment_score DOUBLE PRECISION,
            confidence      DOUBLE PRECISION,
            horizon         TEXT,
            reason          TEXT,
            model           TEXT NOT NULL,
            prompt_version  TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            delivery_status TEXT NOT NULL DEFAULT 'pending',
            sentiment_id    TEXT,
            delivered_at    TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_sentiments_transcript ON cnbc.sentiments (transcript_id)"
    )
    op.execute(
        "CREATE INDEX ix_sentiments_delivery ON cnbc.sentiments (delivery_status)"
    )
    op.execute("CREATE INDEX ix_sentiments_subject ON cnbc.sentiments (subject)")

    # -- referenced entities (LLM pass 3) --
    op.execute(
        """
        CREATE TABLE cnbc.referenced_entities (
            id               SERIAL PRIMARY KEY,
            transcript_id    INTEGER NOT NULL REFERENCES cnbc.transcripts(id),
            raw_mention      TEXT NOT NULL,
            entity_type      TEXT NOT NULL,
            company_name     TEXT,
            ticker           TEXT,
            speaker          TEXT,
            direction        TEXT,
            confidence       DOUBLE PRECISION,
            context          TEXT,
            model            TEXT NOT NULL,
            prompt_version   TEXT NOT NULL,
            idempotency_key  TEXT NOT NULL UNIQUE,
            watchlist_status TEXT NOT NULL DEFAULT 'pending',
            submitted_at     TIMESTAMPTZ,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_entities_transcript ON cnbc.referenced_entities (transcript_id)"
    )
    op.execute(
        "CREATE INDEX ix_entities_watchlist ON cnbc.referenced_entities (watchlist_status)"
    )
    op.execute("CREATE INDEX ix_entities_ticker ON cnbc.referenced_entities (ticker)")

    # -- daily run log + heartbeat --
    op.execute(
        """
        CREATE TABLE cnbc.ingest_runs (
            id                  SERIAL PRIMARY KEY,
            run_date            DATE NOT NULL UNIQUE,
            started_at          TIMESTAMPTZ,
            completed_at        TIMESTAMPTZ,
            status              TEXT NOT NULL DEFAULT 'running',
            shows_processed     INTEGER NOT NULL DEFAULT 0,
            transcripts_fetched INTEGER NOT NULL DEFAULT 0,
            distilled           INTEGER NOT NULL DEFAULT 0,
            reprocessed         INTEGER NOT NULL DEFAULT 0,
            sentiments_sent     INTEGER NOT NULL DEFAULT 0,
            entities_submitted  INTEGER NOT NULL DEFAULT 0,
            failures            INTEGER NOT NULL DEFAULT 0,
            heartbeat_at        TIMESTAMPTZ
        )
        """
    )

    # -- archive.org discovery watermark --
    op.execute(
        """
        CREATE TABLE cnbc.ingest_cursor (
            id             SERIAL PRIMARY KEY,
            collection     TEXT NOT NULL UNIQUE,
            last_addeddate TIMESTAMPTZ,
            last_identifier TEXT,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS cnbc CASCADE")
