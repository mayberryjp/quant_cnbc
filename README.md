# quant_cnbc

CNBC transcript ingestion, LLM distillation, and sentiment + watchlist producer.

`quant_cnbc` is a persistent, supervised service that ingests the previous day's
CNBC show transcripts from the [archive.org `TV-CNBC`](https://archive.org/details/TV-CNBC)
collection, distills them with a local, configurable LLM, then runs **separate
structured LLM passes** to derive sentiment and referenced tickers/companies. It
fans those out to the [`quant_sentiment`](https://github.com/mayberryjp/quant_sentiment)
API and the [`quant_signals`](https://github.com/mayberryjp/quant_signals)
watchlist, and persists everything in PostgreSQL.

See [docs/SPEC.md](docs/SPEC.md) for the full specification.

## Quick Start

```bash
pip install -e ".[dev]"

# Apply database migrations
alembic upgrade head

# Run the API server
python -m app.main

# Run the ingest worker (separate process)
python -m app.services.ingest_worker --once

# Run tests
pytest -v
```

## Architecture

1. **Discover + fetch** — poll the `TV-CNBC` collection for newly added items
   (cursor on `addeddate`), download captions, dedup by archive item identifier.
2. **Distill (LLM pass 1)** — transcript → structured summary JSON.
3. **Sentiment (LLM pass 2)** — distillation → structured sentiment JSON → `quant_sentiment`.
4. **Entities (LLM pass 3)** — distillation → every referenced ticker/company
   (LLM resolves company→ticker) → `quant_signals` watchlist.

Outputs are **version-scoped** by model + prompt version, so transcripts can be
**recalculated** after a model/prompt upgrade and land downstream as fresh records.

## Ports

| Service | Port |
|---|---|
| quant_signals (watchlist) | 8016 |
| quant_sentiment | 8017 |
| quant_cnbc (this service) | 8019 |

## Configuration

Copy `.env.example` to `.env` and adjust. CNBC-specific settings use the `CNBC_`
prefix; `DATABASE_URL` / `API_PORT` are read unprefixed.
