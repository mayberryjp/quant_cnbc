"""Slice 2 tests: archive.org parsing, client, discovery, fetch."""

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import respx

from app.models.domain import IngestCursor, Transcript
from app.services.archive_client import (
    ArchiveClient,
    content_hash,
    normalize_caption,
    parse_identifier,
    pick_caption_file,
)
from app.services.transcript_fetcher import discover_new_items, fetch_transcript

SRT = """1
00:00:01,000 --> 00:00:04,000
Jim Cramer says buy Apple.

2
00:00:04,000 --> 00:00:07,000
Nvidia is the AI leader.
"""


class TestParsing:
    def test_parse_identifier(self):
        slug, d, start = parse_identifier("CNBC_20260702_220000_Mad_Money")
        assert slug == "Mad_Money"
        assert d == date(2026, 7, 2)
        assert start == datetime(2026, 7, 2, 22, 0, tzinfo=timezone.utc)

    def test_parse_identifier_bad(self):
        assert parse_identifier("not-a-cnbc-id") == (None, None, None)

    def test_normalize_caption_strips_srt(self):
        text = normalize_caption(SRT)
        assert "-->" not in text
        assert text == "Jim Cramer says buy Apple. Nvidia is the AI leader."

    def test_pick_caption_prefers_srt(self):
        files = [{"name": "x.mp4"}, {"name": "show.djvu.txt"}, {"name": "show.srt"}]
        assert pick_caption_file(files) == "show.srt"

    def test_content_hash_stable(self):
        assert content_hash("abc") == content_hash("abc")


# ---------------------------------------------------------------------------
# In-memory fakes for orchestration tests
# ---------------------------------------------------------------------------
class FakeTranscriptRepo:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self._seq = 0

    def upsert_discovered(self, batch):
        n = 0
        for it in batch:
            aid = it["archive_identifier"]
            if aid not in self.items:
                self._seq += 1
                self.items[aid] = {**it, "id": self._seq, "status": "discovered"}
                n += 1
        return n

    def set_status(self, tid, status, last_error=None, bump_attempts=False):
        for row in self.items.values():
            if row["id"] == tid:
                row["status"] = status
                row["last_error"] = last_error

    def mark_fetched(self, tid, *, raw_text, content_hash, caption_file):
        for row in self.items.values():
            if row["id"] == tid:
                row.update(status="fetched", raw_text=raw_text,
                           content_hash=content_hash, caption_file=caption_file)


class FakeRunRepo:
    def __init__(self):
        self.cursor: IngestCursor | None = None

    def get_cursor(self, collection):
        return self.cursor

    def set_cursor(self, collection, *, last_addeddate, last_identifier):
        self.cursor = IngestCursor(collection=collection, last_addeddate=last_addeddate,
                                   last_identifier=last_identifier)


class TestDiscoveryAndFetch:
    @respx.mock
    def test_search_and_discover(self):
        respx.get("https://archive.org/advancedsearch.php").mock(
            return_value=httpx.Response(200, json={"response": {"docs": [
                {"identifier": "CNBC_20260702_220000_Mad_Money", "title": "Mad Money",
                 "date": "2026-07-02T00:00:00Z", "addeddate": "2026-07-02T22:30:00Z"},
                {"identifier": "CNBC_20260702_100000_Squawk_Box", "title": "Squawk Box",
                 "date": "2026-07-02T00:00:00Z", "addeddate": "2026-07-02T10:30:00Z"},
            ]}})
        )
        client = ArchiveClient(rate_limit=0)
        trepo, rrepo = FakeTranscriptRepo(), FakeRunRepo()
        n = discover_new_items(client, trepo, rrepo, rows=100)
        assert n == 2
        assert rrepo.cursor.last_identifier == "CNBC_20260702_220000_Mad_Money"  # max addeddate
        # Re-running discovers nothing new (dedup by identifier).
        assert discover_new_items(client, trepo, rrepo, rows=100) == 0

    @respx.mock
    def test_discover_allowlist_filters(self):
        respx.get("https://archive.org/advancedsearch.php").mock(
            return_value=httpx.Response(200, json={"response": {"docs": [
                {"identifier": "CNBC_20260702_220000_Mad_Money", "addeddate": "2026-07-02T22:30:00Z"},
                {"identifier": "CNBC_20260702_100000_Squawk_Box", "addeddate": "2026-07-02T10:30:00Z"},
            ]}})
        )
        client = ArchiveClient(rate_limit=0)
        trepo, rrepo = FakeTranscriptRepo(), FakeRunRepo()
        n = discover_new_items(client, trepo, rrepo, rows=100, allowlist=["mad-money"])
        assert n == 1
        assert "CNBC_20260702_220000_Mad_Money" in trepo.items

    @respx.mock
    def test_discover_allowlist_matches_display_names(self):
        # Allow-list given as human show names (with spaces); archive slugs use
        # underscores. They must normalize equal, "Squawk Box" must not match the
        # distinct "Squawk Box Europe", and entertainment reruns are excluded.
        respx.get("https://archive.org/advancedsearch.php").mock(
            return_value=httpx.Response(200, json={"response": {"docs": [
                {"identifier": "CNBC_20260702_100000_Squawk_Box", "addeddate": "2026-07-02T10:30:00Z"},
                {"identifier": "CNBC_20260702_080000_Squawk_Box_Europe", "addeddate": "2026-07-02T08:30:00Z"},
                {"identifier": "CNBC_20260702_030000_The_Profit", "addeddate": "2026-07-02T03:30:00Z"},
            ]}})
        )
        client = ArchiveClient(rate_limit=0)
        trepo, rrepo = FakeTranscriptRepo(), FakeRunRepo()
        n = discover_new_items(
            client, trepo, rrepo, rows=100,
            allowlist=["Squawk Box", "Squawk Box Europe"],
        )
        assert n == 2
        assert "CNBC_20260702_100000_Squawk_Box" in trepo.items
        assert "CNBC_20260702_080000_Squawk_Box_Europe" in trepo.items
        assert "CNBC_20260702_030000_The_Profit" not in trepo.items

    @respx.mock
    def test_fetch_transcript_downloads_and_normalizes(self):
        aid = "CNBC_20260702_220000_Mad_Money"
        respx.get(f"https://archive.org/metadata/{aid}").mock(
            return_value=httpx.Response(200, json={"files": [
                {"name": "video.mp4"}, {"name": f"{aid}.srt", "format": "SubRip"},
            ]})
        )
        respx.get(f"https://archive.org/download/{aid}/{aid}.srt").mock(
            return_value=httpx.Response(200, text=SRT)
        )
        client = ArchiveClient(rate_limit=0)
        trepo = FakeTranscriptRepo()
        trepo.upsert_discovered([{
            "archive_identifier": aid, "show_slug": "Mad_Money",
            "air_date": date(2026, 7, 2), "broadcast_start": None,
            "title": "Mad Money", "source_url": f"https://archive.org/details/{aid}",
            "archive_addeddate": None,
        }])
        t = Transcript(id=1, archive_identifier=aid, air_date=date(2026, 7, 2),
                       source_url=f"https://archive.org/details/{aid}")
        assert fetch_transcript(client, trepo, t) is True
        row = trepo.items[aid]
        assert row["status"] == "fetched"
        assert "Jim Cramer says buy Apple." in row["raw_text"]

    @respx.mock
    def test_fetch_transcript_no_caption(self):
        aid = "CNBC_20260702_220000_Mad_Money"
        respx.get(f"https://archive.org/metadata/{aid}").mock(
            return_value=httpx.Response(200, json={"files": [{"name": "video.mp4"}]})
        )
        client = ArchiveClient(rate_limit=0)
        trepo = FakeTranscriptRepo()
        trepo.items[aid] = {"id": 1, "archive_identifier": aid, "status": "discovered"}
        t = Transcript(id=1, archive_identifier=aid, air_date=date(2026, 7, 2),
                       source_url=f"https://archive.org/details/{aid}")
        assert fetch_transcript(client, trepo, t) is False
        assert trepo.items[aid]["status"] == "failed"
