"""Transcript discovery + fetch orchestration (archive.org TV-CNBC)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from app.services.archive_client import (
    ArchiveClient,
    content_hash,
    normalize_caption,
    pick_caption_file,
)

log = logging.getLogger("quant_cnbc.fetcher")

_SEPARATORS = re.compile(r"[\s_-]+")


def _norm(token: str) -> str:
    """Normalize a show name/slug for allow-list matching.

    Spaces, hyphens and underscores are treated as equivalent separators so a
    human-entered name (``"Squawk Box"``), a hyphen slug (``"squawk-box"``) and
    the archive identifier token (``"Squawk_Box"``) all compare equal.
    """
    return _SEPARATORS.sub("_", token.strip().lower()).strip("_")


def discover_new_items(
    client: ArchiveClient,
    transcript_repo,
    run_repo,
    *,
    collection: str = "TV-CNBC",
    lookback_days: int = 1,
    overlap_hours: int = 24,
    allowlist: list[str] | None = None,
    max_pages: int = 50,
    rows: int = 100,
) -> int:
    """Discover items added since the cursor; upsert as ``discovered``; advance cursor."""
    cursor = run_repo.get_cursor(collection)
    if cursor and cursor.last_addeddate:
        since = cursor.last_addeddate - timedelta(hours=overlap_hours)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    allow = {_norm(a) for a in (allowlist or [])}
    discovered = 0
    max_added: datetime | None = cursor.last_addeddate if cursor else None
    last_identifier: str | None = cursor.last_identifier if cursor else None

    for page in range(1, max_pages + 1):
        items = client.search(since=since, rows=rows, page=page)
        if not items:
            break
        batch = []
        for it in items:
            if allow and (it.show_slug is None or _norm(it.show_slug) not in allow):
                continue
            batch.append({
                "archive_identifier": it.identifier,
                "show_slug": it.show_slug,
                "air_date": it.air_date,
                "broadcast_start": it.broadcast_start,
                "title": it.title,
                "source_url": it.source_url,
                "archive_addeddate": it.added_at,
            })
            if it.added_at and (max_added is None or it.added_at > max_added):
                max_added = it.added_at
                last_identifier = it.identifier
        discovered += transcript_repo.upsert_discovered(batch)
        if len(items) < rows:
            break

    if max_added is not None:
        run_repo.set_cursor(collection, last_addeddate=max_added, last_identifier=last_identifier)
    log.info("discovery: %d new items (collection=%s)", discovered, collection)
    return discovered


def fetch_transcript(client: ArchiveClient, transcript_repo, transcript) -> bool:
    """Download + normalize a transcript's caption; mark it ``fetched``.

    Returns True on success. Failures are recorded on the row and never raise.
    """
    try:
        files = client.list_files(transcript.archive_identifier)
        caption = pick_caption_file(files)
        if not caption:
            transcript_repo.set_status(
                transcript.id, "failed",
                last_error="no caption/transcript file in archive item",
                bump_attempts=True,
            )
            return False
        raw = client.download_file(transcript.archive_identifier, caption)
        normalized = normalize_caption(raw)
        if not normalized:
            transcript_repo.set_status(
                transcript.id, "failed",
                last_error="caption normalized to empty text", bump_attempts=True,
            )
            return False
        transcript_repo.mark_fetched(
            transcript.id, raw_text=normalized,
            content_hash=content_hash(normalized), caption_file=caption,
        )
        return True
    except Exception as exc:  # network / archive errors — isolate and retry later
        log.exception("fetch failed for %s", transcript.archive_identifier)
        transcript_repo.set_status(
            transcript.id, "failed", last_error=str(exc)[:500], bump_attempts=True
        )
        return False
