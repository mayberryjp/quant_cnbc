"""Slice 4 tests: sentiment pass + quant_sentiment delivery."""

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import respx

from app.models.domain import DeliveryStatus, Transcript
from app.services.sentiment_pass import (
    SentimentApiClient,
    build_rows,
    extract_sentiment,
    idempotency_key,
)

AID = "CNBC_20260702_220000_Mad_Money"


def _transcript():
    return Transcript(
        id=1, archive_identifier=AID, show_slug="Mad_Money", air_date=date(2026, 7, 2),
        broadcast_start=datetime(2026, 7, 2, 22, 0, tzinfo=timezone.utc),
        source_url=f"https://archive.org/details/{AID}",
    )


class FakeLLM:
    def __init__(self, response):
        self.response = response

    def complete_json(self, system, user, json_schema=None):
        return self.response, {"total_tokens": 5}


class TestSentimentPass:
    def test_extract_and_build_rows(self):
        llm = FakeLLM({"observations": [
            {"subject_type": "ticker", "subject": "AAPL", "sentiment_label": "bullish",
             "sentiment_score": 0.8, "confidence": 0.7, "horizon": "5d", "reason": "breakout"},
            {"subject_type": "market", "subject": "ALL", "sentiment_label": "neutral"},
        ]})
        out, _ = extract_sentiment(llm, "summary text")
        rows = build_rows(_transcript(), out, model="m1", prompt_version="v1")
        assert len(rows) == 2
        assert rows[0].idempotency_key == idempotency_key(AID, "AAPL", "m1", "v1")
        assert rows[0].subject == "AAPL"

    @respx.mock
    def test_deliver_accepted(self):
        respx.post("http://sentiment.local/sentiment").mock(
            return_value=httpx.Response(201, json={"sentiment_id": "sentiment:cnbc:x"})
        )
        client = SentimentApiClient(url="http://sentiment.local/sentiment", retries=0)
        out = FakeLLM({"observations": [
            {"subject": "AAPL", "sentiment_label": "bullish"}]}).complete_json("", "")[0]
        from app.models.llm_schemas import SentimentOutput
        rows = build_rows(_transcript(), SentimentOutput.model_validate(out), model="m1", prompt_version="v1")
        status, sid = client.deliver(rows[0], _transcript())
        assert status == DeliveryStatus.sent
        assert sid == "sentiment:cnbc:x"

    @respx.mock
    def test_deliver_duplicate(self):
        respx.post("http://sentiment.local/sentiment").mock(
            return_value=httpx.Response(200, json={"sentiment_id": "sentiment:cnbc:x"})
        )
        client = SentimentApiClient(url="http://sentiment.local/sentiment", retries=0)
        from app.models.llm_schemas import SentimentObservation, SentimentOutput
        out = SentimentOutput(observations=[SentimentObservation(subject="AAPL", sentiment_label="bullish")])
        rows = build_rows(_transcript(), out, model="m1", prompt_version="v1")
        status, _ = client.deliver(rows[0], _transcript())
        assert status == DeliveryStatus.duplicate

    @respx.mock
    def test_deliver_retries_5xx_then_succeeds(self):
        respx.post("http://sentiment.local/sentiment").mock(
            side_effect=[httpx.Response(500), httpx.Response(201, json={"sentiment_id": "s"})]
        )
        client = SentimentApiClient(url="http://sentiment.local/sentiment", retries=1, backoff=0)
        from app.models.llm_schemas import SentimentObservation, SentimentOutput
        out = SentimentOutput(observations=[SentimentObservation(subject="AAPL", sentiment_label="bullish")])
        rows = build_rows(_transcript(), out, model="m1", prompt_version="v1")
        status, _ = client.deliver(rows[0], _transcript())
        assert status == DeliveryStatus.sent

    @respx.mock
    def test_deliver_validation_error_is_failed(self):
        respx.post("http://sentiment.local/sentiment").mock(return_value=httpx.Response(422, json={}))
        client = SentimentApiClient(url="http://sentiment.local/sentiment", retries=0)
        from app.models.llm_schemas import SentimentObservation, SentimentOutput
        out = SentimentOutput(observations=[SentimentObservation(subject="AAPL", sentiment_label="bullish")])
        rows = build_rows(_transcript(), out, model="m1", prompt_version="v1")
        status, _ = client.deliver(rows[0], _transcript())
        assert status == DeliveryStatus.failed
