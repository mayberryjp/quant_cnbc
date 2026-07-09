"""Persistent, supervised ingest worker: daily sleep/wake loop + reprocessing.

Usage:
    python -m app.services.ingest_worker [--wake-time HH:MM] [--interval N]
        [--once] [--date YYYY-MM-DD]
        [--reprocess <archive_identifier>]
        [--reprocess-stale [--show S] [--from-date D] [--to-date D]]
        [--force [--show S] [--from-date D] [--to-date D]]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta

from app.config import settings
from app.services.archive_client import ArchiveClient
from app.services.entity_pass import WatchlistApiClient
from app.services.llm_client import LLMClient
from app.services.pipeline import Pipeline
from app.services.sentiment_pass import SentimentApiClient

logger = logging.getLogger("quant_cnbc.worker")


def build_pipeline(engine=None) -> Pipeline:
    """Construct a Pipeline wired to real repositories and API clients."""
    from app import db, dependencies as deps

    engine = engine or db.get_engine()
    return Pipeline(
        transcript_repo=deps.transcript_repo(engine),
        distillation_repo=deps.distillation_repo(engine),
        sentiment_repo=deps.sentiment_repo(engine),
        entity_repo=deps.entity_repo(engine),
        run_repo=deps.run_repo(engine),
        archive_client=ArchiveClient(
            base_url=settings.archive_base_url, collection=settings.archive_collection,
            rate_limit=settings.archive_rate_limit,
        ),
        llm_client=LLMClient(
            base_url=settings.llm_base_url, model=settings.llm_model,
            api_key=settings.llm_api_key, timeout=settings.llm_timeout,
            max_tokens=settings.llm_max_tokens, json_mode=settings.llm_json_mode,
        ),
        sentiment_api=SentimentApiClient(
            url=settings.sentiment_api_url, api_key=settings.sentiment_api_key,
            source=settings.watchlist_source, timeout=settings.sentiment_timeout,
            retries=settings.http_retries, backoff=settings.retry_backoff,
        ),
        watchlist_api=WatchlistApiClient(
            url=settings.watchlist_api_url, api_key=settings.watchlist_api_key,
            source=settings.watchlist_source, signal_type=settings.watchlist_signal_type,
            timeout=settings.watchlist_timeout, retries=settings.http_retries,
            backoff=settings.retry_backoff,
        ),
        model=settings.llm_model,
        distill_prompt_version=settings.distill_prompt_version,
        sentiment_prompt_version=settings.sentiment_prompt_version,
        entity_prompt_version=settings.entity_prompt_version,
        collection=settings.archive_collection,
        lookback_days=settings.ingest_lookback_days,
        overlap_hours=settings.archive_overlap_hours,
        allowlist=settings.show_allowlist,
        max_attempts=settings.max_attempts,
    )


def seconds_until_wake(wake_time: str, now: datetime | None = None) -> float:
    """Seconds from ``now`` until the next occurrence of HH:MM (local)."""
    now = now or datetime.now()
    try:
        hh, mm = (int(x) for x in wake_time.split(":", 1))
    except ValueError:
        return float(settings.ingest_interval)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _reprocess_stale(pipeline: Pipeline, args) -> int:
    """Reprocess saved transcripts matching the filters.

    By default only *stale* items (whose current distillation predates the
    configured model/prompt) are reprocessed. With ``--force`` every matching
    transcript that has saved ``raw_text`` is reprocessed, even ``done`` ones.
    """
    force = getattr(args, "force", False)
    candidates = pipeline.transcripts.reprocess_candidates(
        show=args.show, from_date=args.from_date, to_date=args.to_date,
        only_stale=not force, current_model=settings.llm_model,
        current_prompt=settings.distill_prompt_version,
    )
    for t in candidates:
        pipeline.reprocess(t)
    logger.info(
        "reprocessed %d transcript(s) (%s)",
        len(candidates), "forced" if force else "stale",
    )
    return len(candidates)


def run_worker(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr, force=True,
    )
    parser = argparse.ArgumentParser(prog="quant_cnbc.ingest_worker")
    parser.add_argument("--wake-time", default=settings.ingest_wake_time)
    parser.add_argument("--interval", type=int, default=settings.ingest_interval)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--date", type=lambda s: date.fromisoformat(s), default=None)
    parser.add_argument("--reprocess", default=None, metavar="ARCHIVE_IDENTIFIER")
    parser.add_argument("--reprocess-stale", action="store_true")
    parser.add_argument(
        "--force", action="store_true",
        help="reprocess ALL matching transcripts (even 'done'), not just stale ones",
    )
    parser.add_argument("--show", default=None)
    parser.add_argument("--from-date", type=lambda s: date.fromisoformat(s), default=None)
    parser.add_argument("--to-date", type=lambda s: date.fromisoformat(s), default=None)
    args = parser.parse_args(argv)

    pipeline = build_pipeline()

    if args.reprocess:
        t = pipeline.transcripts.get_by_identifier(args.reprocess)
        if t is None:
            logger.error("unknown archive_identifier: %s", args.reprocess)
            sys.exit(1)
        pipeline.reprocess(t)
        return
    if args.reprocess_stale or args.force:
        _reprocess_stale(pipeline, args)
        return
    if args.once:
        totals = pipeline.run(args.date)
        logger.info("single pass complete: %s", dict(totals))
        return

    logger.info("ingest worker starting (wake-time=%s)", args.wake_time)
    while True:
        try:
            totals = pipeline.run(args.date)
            logger.info("run complete: %s", dict(totals))
        except Exception:
            logger.exception("run cycle failed - will retry next wake")
        time.sleep(seconds_until_wake(args.wake_time))


if __name__ == "__main__":
    run_worker()
