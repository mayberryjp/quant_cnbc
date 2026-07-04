# quant_cnbc — Engineering Specification

> **Status:** Proposed
> **Type:** Project specification / initial build issue
> **Related services:** [`quant_signals`](https://github.com/mayberryjp/quant_signals) (watchlist) · [`quant_sentiment`](https://github.com/mayberryjp/quant_sentiment) (sentiment API)

## 1. Summary

`quant_cnbc` is a persistent, supervised Python service that ingests the
previous day's CNBC show transcripts from [archive.org](https://archive.org),
distills them with a **local, configurable LLM**, then — via **separate,
structured LLM passes** — derives a **sentiment** JSON object and a
**referenced-entities** JSON object (every ticker or company mentioned, with
company→ticker translation done by the LLM itself). It fans those out to the
`quant_sentiment` API and the `quant_signals` watchlist, and persists all
artifacts in **PostgreSQL**. Extraction is done *by the model returning
structured JSON* — the service does schema validation, not bespoke NLP.

The service is a **daily batch producer**. It runs as a long-lived process under
`supervisord` that sleeps until its scheduled wake time, retrieves and processes
the prior day's transcripts, records a heartbeat, and returns to sleep.

It intentionally reuses the architecture, project layout, dependency stack, and
operational conventions established in `quant_signals` (Bottle + waitress,
SQLAlchemy 2 + Alembic + `psycopg`, Pydantic v2 + `pydantic-settings`,
`supervisord`, slice-based development and testing).

## 2. Goals & Non-Goals

### Goals
1. Retrieve the previous day's transcripts for a configurable set of CNBC shows from archive.org.
2. Distill each transcript via a local LLM with a **configurable model** (OpenAI-compatible endpoint).
3. Derive **sentiment** as a structured JSON object from the distillation (a separate LLM pass) and deliver it to the `quant_sentiment` API.
4. Derive **referenced entities** as a structured JSON object — every ticker **or** company mentioned, with the LLM translating company names to tickers — and submit each to the `quant_signals` **watchlist**.
5. Persist all raw + distilled artifacts in PostgreSQL.
6. Operate as a **persistent, self-scheduling process** (sleep/wake daily) managed by `supervisord`.

### Non-Goals (initial milestone)
- No trade execution or broker integration.
- No frontend UI (operational read API only).
- No real-time / streaming transcript ingestion (daily batch only).
- No model hosting — the LLM is an external, already-running local endpoint.
- No ownership of the sentiment API or the watchlist service (they are external contracts).
- No audio/video transcription — transcripts are consumed as text from archive.org.

## 3. Architecture Overview

```mermaid
flowchart TD
    A[archive.org<br/>CNBC show captures] -->|fetch prior-day transcripts| B[Transcript Fetcher]
    B --> C[(Postgres: transcripts)]
    B --> D[Distiller<br/>LLM pass 1]
    D -->|distill prompt| L[Local LLM<br/>OpenAI-compatible<br/>JSON mode]
    D --> E[(Postgres: distillations)]
    E --> F[Sentiment pass<br/>LLM pass 2 → JSON]
    E --> G[Entity pass<br/>LLM pass 3 → JSON<br/>company→ticker]
    F -->|structured JSON| L
    G -->|structured JSON| L
    F --> H[(Postgres: sentiments)]
    F -->|POST /sentiment| I[quant_sentiment API]
    G --> J[(Postgres: referenced_entities)]
    G -->|POST /signals| K[quant_signals watchlist]
    W[Daily Ingest Worker<br/>sleep/wake loop + heartbeat] --> B
    subgraph supervisord
      W
      API[Ops/Read API<br/>Bottle + waitress]
      MIG[alembic upgrade head]
    end
```

**Processing pipeline (per daily run):**

1. Determine target date (default: previous calendar day) and the configured show list.
2. For each show, locate + fetch the transcript(s) from archive.org.
3. Persist the raw transcript, deduped by the archive.org `archive_identifier` (see §6A).
4. **LLM pass 1 (distill):** send the transcript to the LLM; store the distilled summary/segments.
5. **LLM pass 2 (sentiment):** send the *distillation* back to the LLM for a structured sentiment JSON object → validate → persist → deliver to the `quant_sentiment` API.
6. **LLM pass 3 (entities):** send the *distillation* back to the LLM for a structured JSON list of every referenced ticker/company (LLM resolves company→ticker) → validate → persist → submit each to the `quant_signals` watchlist.
7. Record run status + heartbeat; sleep until the next scheduled wake.

## 4. Tech Stack & Conventions

Mirrors `quant_signals` so both services share operational muscle memory.

| Concern | Choice |
|---|---|
| Language | Python `>=3.11` (Docker base `python:3.12-slim`) |
| Web framework | `bottle` served by `waitress` (`python -m app.main`) |
| Data layer | `SQLAlchemy>=2.0` + `psycopg[binary]>=3.1` (raw `text()` SQL, per repo convention) |
| Migrations | `alembic>=1.13` — raw SQL in `op.execute`, dedicated schema, service-scoped `version_table` |
| Models / validation | `pydantic>=2.7` + `pydantic-settings>=2.3` |
| HTTP client | `httpx>=0.27` (archive.org, LLM, sentiment API, watchlist) |
| Process supervision | `supervisord` (`supervisord.conf`) |
| Tests | `pytest`, `webtest`, `respx`/`httpx` mock transport |
| Packaging | `pyproject.toml` (setuptools) + `requirements.txt` |

### Proposed dependencies (`pyproject.toml`)
```toml
dependencies = [
    "alembic>=1.13",
    "bottle>=0.13.0",
    "psycopg[binary]>=3.1",
    "SQLAlchemy>=2.0",
    "pydantic>=2.7.0",
    "pydantic-settings>=2.3.0",
    "httpx>=0.27",
    "waitress>=3.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0.0", "webtest>=3.0.0", "respx>=0.21.0"]
```

## 5. Repository Layout

```
quant_cnbc/
├── .github/workflows/
├── alembic/
│   ├── env.py                     # DATABASE_URL-driven, version_table=alembic_version_cnbc
│   └── versions/
│       └── 0001_cnbc_schema.py    # raw SQL: CREATE SCHEMA cnbc + tables + indexes
├── app/
│   ├── __init__.py
│   ├── main.py                    # Bottle app; merges route sub-apps; waitress serve
│   ├── config.py                  # pydantic-settings Settings (env_prefix "CNBC_")
│   ├── db.py                      # get_engine() from DATABASE_URL
│   ├── dependencies.py            # get_repo() / get_session()
│   ├── models/
│   │   ├── domain.py              # pydantic domain models + enums
│   │   ├── llm_schemas.py         # pydantic schemas for structured LLM outputs (distill/sentiment/entities)
│   │   ├── requests.py            # API request schemas
│   │   └── responses.py           # API response schemas
│   ├── routes/
│   │   ├── health.py              # /cnbc/health, /cnbc/ready, /cnbc/stats
│   │   ├── transcripts.py         # read API + reprocess triggers (/transcripts/{id}/reprocess, /reprocess)
│   │   └── entities.py            # read API for referenced entities + sentiments
│   ├── services/
│   │   ├── archive_client.py      # archive.org retrieval
│   │   ├── transcript_fetcher.py  # orchestrates prior-day show fetch + dedup
│   │   ├── llm_client.py           # OpenAI-compatible LLM client, JSON/structured-output mode
│   │   ├── distiller.py            # LLM pass 1: transcript → distillation JSON
│   │   ├── sentiment_pass.py       # LLM pass 2: distillation → structured sentiment JSON + /sentiment delivery
│   │   ├── entity_pass.py          # LLM pass 3: distillation → structured entities JSON (company→ticker) + watchlist submission
│   │   ├── reprocessor.py          # recalculate a saved transcript through passes 1–3 (model/prompt upgrades)
│   │   └── ingest_worker.py       # persistent daily sleep/wake loop, heartbeat, reprocess triggers
│   └── repository/
│       ├── transcripts.py
│       ├── distillations.py
│       ├── sentiments.py
│       ├── entities.py
│       └── runs.py                # ingest run log, heartbeat, discovery cursor
├── docs/
│   ├── SPEC.md                    # this document
│   ├── data_model.md              # Postgres schema reference
│   ├── integrations.md            # archive.org / LLM / sentiment / watchlist contracts
│   └── runbook.md                 # operational commands and troubleshooting
├── tests/
│   ├── conftest.py
│   └── test_slice*_*.py
├── .dockerignore
├── .env.example
├── Dockerfile
├── README.md
├── alembic.ini
├── docker-compose.yml
├── pyproject.toml
├── requirements.txt
└── supervisord.conf
```

## 6. Data Model (PostgreSQL — schema `cnbc`)

All tables live in a dedicated `cnbc` schema. Migrations use raw SQL via
`op.execute` and a service-scoped Alembic version table
(`alembic_version_cnbc`), matching `quant_signals`.

### `cnbc.shows` — catalog of tracked CNBC shows
| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `slug` | TEXT UNIQUE NOT NULL | e.g. `mad-money`, `squawk-box` |
| `display_name` | TEXT NOT NULL | |
| `archive_query` | TEXT | archive.org identifier/search pattern |
| `enabled` | BOOLEAN NOT NULL DEFAULT true | |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

### `cnbc.transcripts` — raw retrieved transcripts
| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `archive_identifier` | TEXT UNIQUE NOT NULL | **canonical dedup/tracking key** — the archive.org item id, e.g. `CNBC_20260702_220000_Mad_Money` |
| `show_id` | INT FK → shows.id | resolved from the identifier's show token |
| `air_date` | DATE NOT NULL | parsed from the identifier (`YYYYMMDD`) |
| `broadcast_start` | TIMESTAMPTZ | parsed from the identifier (`HHMMSS`, UTC) |
| `title` | TEXT | archive.org item title |
| `source_url` | TEXT NOT NULL | `https://archive.org/details/<archive_identifier>` |
| `caption_file` | TEXT | resolved caption/transcript filename within the item |
| `content_hash` | TEXT | sha256 of normalized caption text (secondary dedup) |
| `raw_text` | TEXT | populated once fetched (nullable while `discovered`) |
| `status` | TEXT NOT NULL DEFAULT 'discovered' | state machine: `discovered \| fetched \| distilled \| delivered \| done \| failed` |
| `attempts` | INT NOT NULL DEFAULT 0 | retry counter |
| `last_error` | TEXT | last failure detail |
| `archive_addeddate` | TIMESTAMPTZ | item `addeddate` (drives the discovery cursor) |
| `discovered_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| `fetched_at` / `distilled_at` / `delivered_at` | TIMESTAMPTZ | per-stage completion timestamps |
| | | UNIQUE `(archive_identifier)`, UNIQUE `(content_hash)` |

### `cnbc.distillations` — LLM output (versioned; reprocess-friendly)
Keyed by model + prompt version so a re-run after a model/prompt upgrade **adds a
new distillation** (history preserved) rather than clobbering the old one.
| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `transcript_id` | INT FK → transcripts.id | |
| `model` | TEXT NOT NULL | model name used |
| `prompt_version` | TEXT NOT NULL | distiller prompt version |
| `summary` | TEXT NOT NULL | |
| `key_topics` | JSONB NOT NULL DEFAULT '[]' | |
| `segments` | JSONB NOT NULL DEFAULT '[]' | speaker/guest segments |
| `token_usage` | JSONB | |
| `is_current` | BOOLEAN NOT NULL DEFAULT true | newest wins; prior rows set to false |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| | | UNIQUE `(transcript_id, model, prompt_version)` |

### `cnbc.sentiments` — structured sentiment (LLM pass 2 output; one row per subject)
Fields mirror the `quant_sentiment` `POST /sentiment` contract so delivery is a
near pass-through of the LLM's JSON.
| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `transcript_id` | INT FK → transcripts.id | |
| `subject_type` | TEXT NOT NULL DEFAULT 'ticker' | `ticker \| sector \| theme \| market` |
| `subject` | TEXT NOT NULL | e.g. `AAPL`, `semiconductors`, `ALL` (whole show) |
| `sentiment_label` | TEXT NOT NULL | `bullish \| bearish \| neutral` |
| `sentiment_score` | DOUBLE PRECISION | -1.0 … 1.0 |
| `confidence` | DOUBLE PRECISION | 0.0 … 1.0 |
| `horizon` | TEXT | `intraday \| 1d \| 5d \| 30d` |
| `reason` | TEXT | LLM rationale |
| `model` | TEXT NOT NULL | model that produced this row |
| `prompt_version` | TEXT NOT NULL | sentiment prompt version |
| `idempotency_key` | TEXT NOT NULL | `cnbc:<archive_identifier>:<subject>:<model>:<prompt_version>` (version-scoped, so improved re-runs are new observations) |
| `delivery_status` | TEXT NOT NULL DEFAULT 'pending' | `pending \| sent \| duplicate \| failed` |
| `sentiment_id` | TEXT | id returned by `quant_sentiment` |
| `delivered_at` | TIMESTAMPTZ | |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| | | UNIQUE `(idempotency_key)` |

### `cnbc.referenced_entities` — every ticker/company referenced (LLM pass 3 output)
Any ticker **or** company mentioned goes to the watchlist; the LLM resolves
company names to tickers within the same structured pass.
| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `transcript_id` | INT FK → transcripts.id | |
| `raw_mention` | TEXT NOT NULL | as spoken (e.g. `Apple`, `AAPL`) |
| `entity_type` | TEXT NOT NULL | `ticker \| company` (classified by the LLM) |
| `company_name` | TEXT | normalized company name |
| `ticker` | TEXT | LLM-resolved ticker (nullable if unresolved) |
| `speaker` | TEXT | guest/anchor attribution, if any |
| `direction` | TEXT | `long \| short \| neutral` (if expressed) |
| `confidence` | DOUBLE PRECISION | 0.0 … 1.0 (LLM confidence in the resolution) |
| `context` | TEXT | short quote/rationale |
| `model` | TEXT NOT NULL | model that produced this row |
| `prompt_version` | TEXT NOT NULL | entity prompt version |
| `idempotency_key` | TEXT NOT NULL | `cnbc:<archive_identifier>:<ticker-or-mention>:<model>:<prompt_version>` (version-scoped) |
| `watchlist_status` | TEXT NOT NULL DEFAULT 'pending' | `pending \| submitted \| duplicate \| failed \| unresolved` |
| `submitted_at` | TIMESTAMPTZ | |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| | | UNIQUE `(idempotency_key)` |

### `cnbc.ingest_runs` — daily run log + heartbeat
| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `run_date` | DATE NOT NULL | |
| `started_at` / `completed_at` | TIMESTAMPTZ | |
| `status` | TEXT NOT NULL DEFAULT 'running' | `running \| success \| partial \| failed` |
| `shows_processed` | INT NOT NULL DEFAULT 0 | |
| `transcripts_fetched` | INT NOT NULL DEFAULT 0 | |
| `distilled` / `sentiments_sent` / `entities_submitted` / `failures` | INT | counters |
| `heartbeat_at` | TIMESTAMPTZ | worker liveness |
| | | UNIQUE `(run_date)` |

### `cnbc.ingest_cursor` — archive.org discovery watermark
Single row (per collection) recording how far discovery has advanced, so each
run pulls only **newly added** items instead of rescanning the whole ~4,600-item
collection.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `collection` | TEXT UNIQUE NOT NULL | `TV-CNBC` |
| `last_addeddate` | TIMESTAMPTZ | high-water mark of processed item `addeddate` |
| `last_identifier` | TEXT | last item id seen (tiebreaker / debug) |
| `updated_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |

## 6A. Retrieval & Distillation Tracking (archive.org `TV-CNBC`)

**Problem:** never re-fetch / re-distil items already processed, resume cleanly
after partial failures, and reliably pick up captions that archive.org publishes
hours-to-days after broadcast.

### Canonical identity
Every collection item has a stable, globally-unique id of the form
`CNBC_<YYYYMMDD>_<HHMMSS>_<Show_Name>` (e.g. `CNBC_20260702_220000_Mad_Money`,
`CNBC_20260702_130000_Squawk_on_the_Street`). This `archive_identifier` — **not**
`(show, date)` — is the unit of tracking: one show can produce several items per
day (multi-hour blocks, EU/US editions), so identity is per broadcast item. It is
stored UNIQUE and parsed into `show_id` / `air_date` / `broadcast_start`.

### Two-level tracking
1. **Discovery watermark — `cnbc.ingest_cursor`.** A high-water mark on item
   `addeddate`. Each run scans only `addeddate > last_addeddate` (advancedsearch
   API, sorted `addeddate asc`) rather than the whole collection. A small
   look-back overlap (e.g. `last_addeddate − 24h`) guards against paging/clock
   edges; the UNIQUE identifier makes the overlap harmless.
2. **Per-item state machine — `cnbc.transcripts.status`.** Each discovered item
   advances independently and idempotently:

   ```mermaid
   stateDiagram-v2
     [*] --> discovered
     discovered --> fetched: caption downloaded + hashed
     fetched --> distilled: LLM distillation stored
     distilled --> delivered: sentiment + trades fanned out
     delivered --> done
     discovered --> failed
     fetched --> failed
     distilled --> failed
     failed --> discovered: retry (attempts++)
   ```

   Stage timestamps (`fetched_at`, `distilled_at`, `delivered_at`) plus
   `attempts` / `last_error` make progress and failures directly queryable.

### Run algorithm (each wake)
1. Read `ingest_cursor.last_addeddate` (default `NOW − CNBC_INGEST_LOOKBACK_DAYS`).
2. Page the advancedsearch API for `collection:TV-CNBC AND addeddate:[cursor TO *]`.
3. `INSERT … ON CONFLICT (archive_identifier) DO NOTHING` each item as
   `discovered` (recording `title`, `air_date`, `broadcast_start`, `archive_addeddate`).
4. Select actionable rows (`status IN ('discovered','fetched','distilled')`, or
   `failed` under the retry cap) and drive each through the state machine:
   fetch caption → distil → extract + deliver → `done`.
5. Advance `ingest_cursor.last_addeddate` to the max `addeddate` fully recorded as
   `discovered`; update `ingest_run` counters + heartbeat.

Because discovery is decoupled from processing, "which are retrieved / distilled
already" is answered directly from `cnbc.transcripts`:

```sql
-- already retrieved / how far along?
SELECT status FROM cnbc.transcripts WHERE archive_identifier = :id;

-- fetched but not yet distilled
SELECT archive_identifier FROM cnbc.transcripts WHERE status = 'fetched';

-- everything still outstanding
SELECT archive_identifier, status FROM cnbc.transcripts
 WHERE status <> 'done' ORDER BY broadcast_start;
```

### Reprocessing / recalculation (model or prompt upgrades)
The raw caption is stored, so any transcript can be **recalculated from the saved
text without re-downloading** — essential as local models improve quickly.
Reprocessing re-runs LLM passes 1–3 with the *current* `CNBC_LLM_MODEL` + prompt
versions.

**Version-scoped outputs.** Distillations are keyed by `(transcript_id, model,
prompt_version)` and sentiment/entity idempotency keys embed
`:<model>:<prompt_version>`. So a re-run with an **improved** model/prompt
produces **new** distillation rows and **new** downstream observations (the
`quant_sentiment` / `quant_signals` dedup treats them as fresh — matching those
services' “corrections are new records” rule); a re-run with the **same** config
is a no-op (dedup holds). Prior rows are retained for comparison; the newest
distillation is flagged `is_current`.

**Triggers:**
- **API:** `POST /transcripts/<archive_identifier>/reprocess` (single) and
  `POST /reprocess` with a filter body (`{show, from_date, to_date, only_stale}`)
  for bulk. `only_stale=true` selects transcripts whose current distillation
  `model` / `prompt_version` differ from the configured ones. Optional `from`
  (`distill` | `sentiment` | `entities`) limits which passes re-run; default is all.
- **Worker CLI:** `--reprocess <archive_identifier>` /
  `--reprocess-stale [--show S] [--from-date …] [--to-date …]`.
- **Auto (opt-in):** `CNBC_REPROCESS_ON_PROMPT_CHANGE=true` reprocesses stale
  transcripts during normal wakes.

Mechanically, reprocessing resets the transcript to `fetched` (the saved
`raw_text` is reused) and re-drives distil → sentiment → entities → `done`;
archive.org is never re-hit.

## 7. External Integrations

Full request/response contracts to live in `docs/integrations.md`.

### 7.1 archive.org (transcript source) — collection `TV-CNBC`
Source: <http://archive.org/details/TV-CNBC> (TV News Archive CNBC collection, ~4,600+ items).

- **Item id format:** `CNBC_<YYYYMMDD>_<HHMMSS>_<Show_Name>` (globally unique + stable → canonical dedup/tracking key, see §6A).
- **Discovery (list new items):** advancedsearch/scrape API —
  `GET https://archive.org/advancedsearch.php?q=collection:TV-CNBC AND addeddate:[<cursor> TO *]&fl[]=identifier&fl[]=title&fl[]=date&fl[]=addeddate&sort[]=addeddate asc&rows=…&output=json`.
- **Per-item files:** `GET https://archive.org/metadata/<identifier>` lists caption/transcript files (closed-caption `.srt`/`.vtt`/`*.cc*.txt` / `<identifier>.djvu.txt`); download at `https://archive.org/download/<identifier>/<file>`.
- Read-only, rate-limited (`CNBC_ARCHIVE_RATE_LIMIT`), descriptive `User-Agent`, retried with backoff.

### 7.2 Local LLM — three structured passes (configurable model)
- **OpenAI-compatible** chat/completions endpoint (Ollama, LM Studio, vLLM, …), invoked in **JSON mode / structured outputs** (`response_format=json_schema` when supported, else `json_object` with the schema in the prompt).
- **The model does the extraction — not our code.** Each pass returns a JSON object validated against a Pydantic schema (`app/models/llm_schemas.py`); the service does only schema validation + light normalization (uppercase ticker, clamp scores), never bespoke NLP/regex parsing.
- **Pass 1 — Distill:** transcript → `{summary, key_topics[], segments[]}`. Long transcripts are chunked then reduced.
- **Pass 2 — Sentiment:** *distillation* → `{observations: [{subject_type, subject, sentiment_label, sentiment_score, confidence, horizon, reason}]}` (per ticker/sector/theme + one `market`/`ALL` for the whole show). Maps 1:1 onto the `quant_sentiment` contract (§7.3).
- **Pass 3 — Entities:** *distillation* → `{entities: [{raw_mention, entity_type, company_name, ticker, speaker, direction, confidence, context}]}` — **every ticker or company referenced**, with the LLM translating company names → tickers in the same call.
- Each pass has an independently versioned prompt (`prompt_version`). Config: `CNBC_LLM_BASE_URL`, `CNBC_LLM_MODEL`, `CNBC_LLM_API_KEY`, `CNBC_LLM_TIMEOUT`, `CNBC_LLM_MAX_TOKENS`, plus per-pass prompt-version settings.

### 7.3 quant_sentiment API (downstream) — `POST /sentiment`
Service: [`mayberryjp/quant_sentiment`](https://github.com/mayberryjp/quant_sentiment) (default port `8017`).

- One `POST /sentiment` per row from pass 2. Body (subset of the contract): `source="cnbc"`, `idempotency_key=cnbc:<archive_identifier>:<subject>:<model>:<prompt_version>`, `subject_type`, `subject`, `sentiment_label` (`bullish|bearish|neutral`), `sentiment_score` (−1…1), `confidence`, `horizon`, `reason`, `observed_at` (= `broadcast_start`), `tags`, `metadata` (show, air_date, archive_identifier, guest).
- Status semantics mirror the platform: `201` accepted, `200` duplicate, `422` invalid. Store the returned `sentiment_id`; treat `duplicate` as success.
- Best-effort with retries; failures persist as `delivery_status='failed'` for replay and never abort the pipeline.

### 7.4 quant_signals watchlist (downstream) — `POST /signals`
Endpoint + auth are fully env-driven: `CNBC_WATCHLIST_API_URL`, `CNBC_WATCHLIST_API_KEY`, `CNBC_WATCHLIST_SOURCE` (default `cnbc`), `CNBC_WATCHLIST_SIGNAL_TYPE` (default `cnbc_mention`), `CNBC_WATCHLIST_TIMEOUT`.

- **Every referenced ticker or company (pass 3) goes to the watchlist** — not only explicit "trades". Companies are submitted under their LLM-resolved ticker; entries the LLM cannot resolve are held locally as `unresolved` (not submitted).
- One `POST /signals` per resolved entity: `source=$CNBC_WATCHLIST_SOURCE`, `signal_type=$CNBC_WATCHLIST_SIGNAL_TYPE`, idempotency key `cnbc:<archive_identifier>:<ticker>:<model>:<prompt_version>` (version-scoped, so improved re-runs re-submit). `direction` from the entity if expressed; `reason`=context; `metadata` carries show/air_date/guest/company_name/raw_mention.
- Duplicate submissions are safe (downstream dedups; we also dedup locally via the unique key).

## 8. Configuration (`.env`)

`pydantic-settings` `Settings` with `env_prefix="CNBC_"`; `DATABASE_URL` and
`API_PORT` are read directly (matching `quant_signals`).

```dotenv
# Postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=quant
POSTGRES_USER=quant
POSTGRES_PASSWORD=quant_dev_password
DATABASE_URL=postgresql+psycopg://quant:quant_dev_password@localhost:5432/quant

# Ops API (8016=quant_signals, 8017=quant_sentiment → quant_cnbc uses 8019)
API_PORT=8019

# Ingestion schedule
CNBC_INGEST_WAKE_TIME=06:00          # local time to wake and process prior day
CNBC_INGEST_INTERVAL=86400           # seconds between runs (fallback / catch-up cadence)
CNBC_INGEST_LOOKBACK_DAYS=1
CNBC_SHOWS=mad-money,squawk-box,halftime-report,fast-money

# archive.org
CNBC_ARCHIVE_BASE_URL=https://web.archive.org
CNBC_ARCHIVE_RATE_LIMIT=1.0

# Local LLM (configurable model) — three structured passes
CNBC_LLM_BASE_URL=http://localhost:11434/v1
CNBC_LLM_MODEL=llama3.1:8b
CNBC_LLM_API_KEY=
CNBC_LLM_TIMEOUT=120
CNBC_LLM_MAX_TOKENS=2048
CNBC_LLM_JSON_MODE=true               # request structured/JSON output
CNBC_DISTILL_PROMPT_VERSION=v1
CNBC_SENTIMENT_PROMPT_VERSION=v1
CNBC_ENTITY_PROMPT_VERSION=v1

# Downstream APIs
CNBC_SENTIMENT_API_URL=http://localhost:8017/sentiment   # quant_sentiment
CNBC_SENTIMENT_API_KEY=
# Watchlist (quant_signals) endpoint — all configurable via env
CNBC_WATCHLIST_API_URL=http://localhost:8016/signals     # quant_signals POST /signals
CNBC_WATCHLIST_API_KEY=
CNBC_WATCHLIST_SOURCE=cnbc
CNBC_WATCHLIST_SIGNAL_TYPE=cnbc_mention
CNBC_WATCHLIST_TIMEOUT=30

# Reprocessing / recalculation (models improve quickly)
CNBC_REPROCESS_ON_PROMPT_CHANGE=false    # auto-reprocess stale transcripts on normal wakes
```

## 9. HTTP API (Bottle + waitress)

Small operational + read-only API (no public write endpoints; the worker drives writes).

| Method | Path | Description |
|---|---|---|
| GET | `/cnbc/health` | Liveness (no DB dependency) |
| GET | `/cnbc/ready` | Readiness (DB reachable + fresh run heartbeat) |
| GET | `/cnbc/stats` | Counters + last run summary |
| GET | `/transcripts` | List transcripts (filter by show, date; paginated) |
| GET | `/transcripts/{id}` | Transcript + distillation detail |
| GET | `/entities` | Recent referenced tickers/companies (filter by ticker/date/status) |
| GET | `/sentiments` | Recent sentiments (filter by subject/date/status) |
| POST | `/runs/trigger` | Manually trigger a run for a given date (ops convenience) |
| POST | `/transcripts/{archive_identifier}/reprocess` | Recalculate one transcript from saved text (re-run LLM passes with current model/prompts) |
| POST | `/reprocess` | Bulk recalculate by filter (`show`, `from_date`, `to_date`, `only_stale`, `from`) |

## 10. Persistent Daily Worker & `supervisord`

The worker (`app/services/ingest_worker.py`) is a long-lived supervised process:

- CLI: `python -m app.services.ingest_worker [--wake-time HH:MM] [--interval N] [--once] [--date YYYY-MM-DD] [--reprocess <archive_identifier>] [--reprocess-stale [--show S] [--from-date …] [--to-date …]]`.
- Loop: compute the next wake time → sleep → run the pipeline for the target date → update `ingest_runs` + write `heartbeat_at` → repeat. Exceptions are caught, logged, and recorded as a failed/partial run; the loop always continues.
- `--once` performs a single pass (used in tests / manual backfill), matching the `quant_signals` maintenance worker pattern.
- `--reprocess <id>` / `--reprocess-stale` recalculate **saved** transcripts through the current model/prompts (no archive.org re-fetch); `--reprocess-stale` targets transcripts whose distillation model/prompt differs from the configured versions. See §6A.

### `supervisord.conf` (proposed)
```ini
[supervisord]
nodaemon=true
logfile=/var/log/supervisord.log
loglevel=info

[program:alembic-migrate]
command=alembic upgrade head
directory=/app
autostart=true
autorestart=false
startsecs=0
exitcodes=0
priority=10
environment=PYTHONUNBUFFERED="1"

[program:cnbc-api]
command=bash -c "sleep 5 && exec python3 -m app.main"
directory=/app
autostart=true
autorestart=true
startretries=999
startsecs=5
priority=20
environment=PYTHONUNBUFFERED="1"

[program:cnbc-ingest-worker]
command=bash -c "sleep 5 && exec python3 -m app.services.ingest_worker --wake-time %(ENV_CNBC_INGEST_WAKE_TIME)s"
directory=/app
autostart=true
autorestart=true
startretries=999
startsecs=5
priority=20
environment=PYTHONUNBUFFERED="1"
```

### `Dockerfile` (proposed)
```dockerfile
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git vim procps \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/mayberryjp/quant_cnbc.git .
RUN python3 -m pip install --upgrade pip \
    && python3 -m pip install -e ".[dev]" \
    && python3 -m pip install supervisor
CMD ["supervisord", "-c", "/app/supervisord.conf"]
```

## 11. Idempotency, Dedup & Error Handling

- **Transcript dedup & tracking:** the archive.org `archive_identifier` is the canonical unique key (a show can air multiple items per day, so identity is per-item, not per-`(show, date)`); a discovery cursor + per-item state machine track retrieval/distillation progress; `content_hash` catches re-published duplicates. See §6A.
- **Distillation:** versioned by `(transcript_id, model, prompt_version)`; the newest is `is_current`. Re-running the same version is a no-op; a new model/prompt adds a new distillation.
- **Structured passes:** the LLM returns validated JSON (pass 2 sentiment, pass 3 entities); the service persists + forwards with minimal normalization.
- **Sentiment idempotency:** `cnbc:<archive_identifier>:<subject>:<model>:<prompt_version>` (honored by `quant_sentiment`).
- **Watchlist idempotency:** `cnbc:<archive_identifier>:<ticker>:<model>:<prompt_version>` — unique locally, honored downstream.
- **Reprocessing:** because idempotency keys embed model + prompt version, recalculating after a model upgrade produces fresh downstream records, while re-running the same version dedups cleanly (see §6A).
- **Best-effort fan-out:** sentiment/watchlist delivery failures are persisted (`*_status='failed'`) and never abort persistence or the run; a replay path re-sends `failed`/`pending` rows on the next cycle.
- **Retries:** external calls use bounded exponential backoff; the LLM and archive.org get generous timeouts.
- **Partial runs:** per-show failures are isolated; the run is marked `partial` and continues.

## 12. Development Plan — Vertical Slices

Each slice ships end-to-end with its own `test_slice<N>_*.py`, matching the
`quant_signals` slice-based approach.

| Slice | Deliverable | Acceptance |
|---|---|---|
| **0** | Scaffold: `pyproject`, `config`, `db`, Bottle `main`, `/cnbc/health`, Alembic baseline, Docker/supervisord skeleton | `pytest` green; health returns `{"status":"ok"}` |
| **1** | Postgres `cnbc` schema migration + domain models + repositories | migration applies; repo CRUD round-trips |
| **2** | archive.org transcript retrieval + dedup + persist | mocked archive.org fetch persists a transcript; dedup verified |
| **3** | LLM pass 1: distillation (configurable model, JSON) + persist | mocked LLM returns valid distill JSON → stored `distillation` |
| **4** | LLM pass 2: structured sentiment JSON + `POST /sentiment` delivery + persist | mocked LLM → validated observations; delivery (201/200/422) + failure/replay covered |
| **5** | LLM pass 3: structured entities JSON (company→ticker) + watchlist submission + persist | mocked LLM → entities; every ticker/company submitted; unresolved held; idempotency covered |
| **6** | Persistent daily worker (sleep/wake) + `ingest_runs` heartbeat | `--once` runs full pipeline; heartbeat written |
| **7** | Read/ops API: `/ready`, `/stats`, `/transcripts`, `/entities`, `/sentiments` | endpoints return expected shapes |
| **8** | Hardening: retries, partial-run handling, validation, Docker/supervisord finalization, docs | resilience + boundary tests green |
| **9** | Reprocessing triggers: `POST /transcripts/{id}/reprocess`, `POST /reprocess` (bulk/stale), worker `--reprocess*`, versioned outputs | recalculating a saved transcript re-runs passes with current model/prompts and emits fresh version-scoped downstream records; same-version re-run is a no-op |

## 13. Testing Strategy

- `pytest` + `webtest` `TestApp` for the Bottle API.
- All external I/O (archive.org, LLM, sentiment API, watchlist) mocked via `respx`/httpx mock transport — **no live network in tests**.
- DB tests run against a disposable Postgres (or transactional rollback fixtures); no live vendor calls.
- Deterministic fixtures for transcripts, LLM outputs, and extracted entities.

## 14. Observability

- Structured stderr logging (`%(asctime)s %(levelname)s %(name)s: %(message)s`), matching `quant_signals`.
- `/cnbc/stats` exposes counters (`transcripts_fetched`, `distilled`, `reprocessed`, `sentiments_sent`, `entities_submitted`, `failures`) and last-run summary.
- `/cnbc/ready` reports DB reachability + worker heartbeat freshness.

## 15. Security Considerations

- All secrets (`DATABASE_URL`, `*_API_KEY`) via environment only; nothing committed. `.env.example` holds placeholders.
- Outbound-only integrations; the read API exposes no secrets and no write surface beyond the ops trigger.
- Respect archive.org rate limits / terms; identify via a proper User-Agent.
- LLM prompts constructed to reduce injection impact; the model returns structured JSON that is schema-validated (Pydantic) before use; resolved tickers are normalized before downstream submission.

## 16. Open Questions

1. ~~**archive.org source**~~ — **Resolved:** collection `TV-CNBC` (<http://archive.org/details/TV-CNBC>), item id format `CNBC_<YYYYMMDD>_<HHMMSS>_<Show_Name>`; tracking model in §6A / §7.1.
2. **Show list:** confirm which shows to track (observed: `Mad_Money`, `Squawk_Box`, `Squawk_Box_Europe`, `Squawk_on_the_Street`, `Power_Lunch`, `The_Exchange`, `Closing_Bell`, `Fast_Money`, …) — track the whole collection or an allow-list?
3. ~~**Watchlist endpoint**~~ — **Resolved:** every referenced ticker **or** company → `quant_signals` `POST /signals` (`signal_type="cnbc_mention"`); company→ticker via the LLM; unresolved held locally. See §7.4.
4. ~~**Sentiment API contract**~~ — **Resolved:** `quant_sentiment` `POST /sentiment` (port `8017`); schema in §7.3, mapped from LLM pass 2.
5. **LLM runtime:** confirm the default local model + endpoint (Ollama vs LM Studio vs vLLM) for defaults.
6. **Schedule semantics:** fixed wake time (e.g. 06:00 local) vs pure interval? Spec supports both; confirm the default.

## 17. Definition of Done

- All 10 slices merged with green tests.
- `docker compose up` brings up the service; migrations apply on boot; worker processes the prior day on schedule and on `--once`.
- Distilled transcripts, sentiments, and referenced entities are persisted in Postgres.
- Sentiment records are delivered to the `quant_sentiment` API; every resolved ticker/company is submitted to the `quant_signals` watchlist (with idempotency).
- A saved transcript can be recalculated on demand (API or worker) after a model/prompt upgrade, producing fresh version-scoped sentiment + watchlist records without re-downloading.
- `README.md`, `docs/data_model.md`, `docs/integrations.md`, and `docs/runbook.md` complete.
