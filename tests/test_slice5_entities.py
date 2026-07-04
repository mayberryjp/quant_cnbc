"""Slice 5 tests: entity pass (company->ticker) + watchlist submission."""

from __future__ import annotations

from datetime import date

import httpx
import respx

from app.models.domain import Transcript, WatchlistStatus
from app.services.entity_pass import (
    WatchlistApiClient,
    build_rows,
    extract_entities,
)

AID = "CNBC_20260702_220000_Mad_Money"


def _transcript():
    return Transcript(
        id=1, archive_identifier=AID, show_slug="Mad_Money", air_date=date(2026, 7, 2),
        source_url=f"https://archive.org/details/{AID}",
    )


class FakeLLM:
    def __init__(self, response):
        self.response = response

    def complete_json(self, system, user, json_schema=None):
        return self.response, {"total_tokens": 5}


class TestEntityPass:
    def test_build_rows_resolved_and_unresolved(self):
        llm = FakeLLM({"entities": [
            {"raw_mention": "Apple", "entity_type": "company", "company_name": "Apple Inc.", "ticker": "aapl"},
            {"raw_mention": "some private startup", "entity_type": "company", "ticker": None},
        ]})
        out, _ = extract_entities(llm, "summary")
        rows = build_rows(_transcript(), out, model="m1", prompt_version="v1")
        assert rows[0].ticker == "AAPL"
        assert rows[0].watchlist_status == WatchlistStatus.pending
        assert rows[1].watchlist_status == WatchlistStatus.unresolved
        assert rows[0].idempotency_key == f"cnbc:{AID}:AAPL:m1:v1"

    def test_build_rows_dedup(self):
        llm = FakeLLM({"entities": [
            {"raw_mention": "Apple", "ticker": "AAPL"},
            {"raw_mention": "AAPL", "ticker": "AAPL"},
        ]})
        out, _ = extract_entities(llm, "summary")
        rows = build_rows(_transcript(), out, model="m1", prompt_version="v1")
        assert len(rows) == 1

    @respx.mock
    def test_submit_resolved_ticker(self):
        respx.post("http://signals.local/signals").mock(return_value=httpx.Response(201, json={}))
        client = WatchlistApiClient(url="http://signals.local/signals", retries=0)
        from app.models.llm_schemas import EntityMention, EntityOutput
        out = EntityOutput(entities=[EntityMention(raw_mention="Apple", ticker="AAPL")])
        rows = build_rows(_transcript(), out, model="m1", prompt_version="v1")
        assert client.submit(rows[0], _transcript()) == WatchlistStatus.submitted

    @respx.mock
    def test_submit_duplicate(self):
        respx.post("http://signals.local/signals").mock(return_value=httpx.Response(200, json={}))
        client = WatchlistApiClient(url="http://signals.local/signals", retries=0)
        from app.models.llm_schemas import EntityMention, EntityOutput
        out = EntityOutput(entities=[EntityMention(raw_mention="Apple", ticker="AAPL")])
        rows = build_rows(_transcript(), out, model="m1", prompt_version="v1")
        assert client.submit(rows[0], _transcript()) == WatchlistStatus.duplicate

    def test_submit_unresolved_skips_http(self):
        # No respx route registered -> if it tried HTTP it would error.
        client = WatchlistApiClient(url="http://signals.local/signals", retries=0)
        from app.models.llm_schemas import EntityMention, EntityOutput
        out = EntityOutput(entities=[EntityMention(raw_mention="startup", ticker=None)])
        rows = build_rows(_transcript(), out, model="m1", prompt_version="v1")
        assert client.submit(rows[0], _transcript()) == WatchlistStatus.unresolved
