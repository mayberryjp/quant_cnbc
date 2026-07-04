"""LLM pass 2 - sentiment. Distillation -> structured sentiment JSON -> quant_sentiment."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.models.domain import DeliveryStatus, Sentiment, Transcript
from app.models.llm_schemas import SentimentOutput
from app.services.http_util import request_with_retry

log = logging.getLogger("quant_cnbc.sentiment")

SENTIMENT_SYSTEM = (
    "You are a market-sentiment classifier. Given a distilled CNBC show summary, "
    "return ONLY a JSON object: "
    '{"observations": [{"subject_type": "ticker|sector|theme|market", '
    '"subject": "AAPL or sector/theme name or ALL", '
    '"sentiment_label": "bullish|bearish|neutral", '
    '"sentiment_score": -1.0..1.0, "confidence": 0.0..1.0, '
    '"horizon": "intraday|1d|5d|30d", "reason": "short rationale"}]}. '
    "Include one observation per ticker/sector/theme discussed, plus one "
    'subject_type "market" with subject "ALL" for the overall show tone.'
)


def idempotency_key(archive_identifier: str, subject: str, model: str, prompt_version: str) -> str:
    return f"cnbc:{archive_identifier}:{subject}:{model}:{prompt_version}"


def extract_sentiment(llm_client, distill_summary: str) -> tuple[SentimentOutput, dict[str, Any]]:
    data, usage = llm_client.complete_json(
        SENTIMENT_SYSTEM, f"Distilled summary:\n{distill_summary}\n\nReturn the JSON object."
    )
    return SentimentOutput.model_validate(data), usage


def build_rows(
    transcript: Transcript, output: SentimentOutput, *, model: str, prompt_version: str,
) -> list[Sentiment]:
    rows: list[Sentiment] = []
    for obs in output.observations:
        rows.append(
            Sentiment(
                transcript_id=transcript.id,
                subject_type=obs.subject_type,
                subject=obs.subject,
                sentiment_label=obs.sentiment_label,
                sentiment_score=obs.sentiment_score,
                confidence=obs.confidence,
                horizon=obs.horizon,
                reason=obs.reason,
                model=model,
                prompt_version=prompt_version,
                idempotency_key=idempotency_key(
                    transcript.archive_identifier, obs.subject, model, prompt_version
                ),
            )
        )
    return rows


class SentimentApiClient:
    """Delivers sentiment rows to the quant_sentiment ``POST /sentiment`` API."""

    def __init__(
        self, *, url: str, api_key: str = "", source: str = "cnbc", timeout: int = 30,
        retries: int = 3, backoff: float = 1.0, client: httpx.Client | None = None,
    ) -> None:
        self.url = url
        self.source = source
        self.retries = retries
        self.backoff = backoff
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.Client(timeout=timeout, headers=headers)

    def deliver(self, s: Sentiment, transcript: Transcript) -> tuple[DeliveryStatus, str | None]:
        body = {
            "source": self.source,
            "idempotency_key": s.idempotency_key,
            "subject_type": s.subject_type.value,
            "subject": s.subject,
            "sentiment_label": s.sentiment_label.value,
            "sentiment_score": s.sentiment_score,
            "confidence": s.confidence,
            "horizon": s.horizon,
            "reason": s.reason or "",
            "observed_at": transcript.broadcast_start.isoformat() if transcript.broadcast_start else None,
            "tags": ["cnbc", transcript.show_slug] if transcript.show_slug else ["cnbc"],
            "metadata": {
                "show": transcript.show_slug,
                "air_date": str(transcript.air_date),
                "archive_identifier": transcript.archive_identifier,
            },
        }
        try:
            resp = request_with_retry(
                lambda: self._client.post(self.url, json=body),
                retries=self.retries, backoff=self.backoff,
            )
        except httpx.HTTPError as exc:
            log.warning("sentiment delivery transport error for %s: %s", s.idempotency_key, exc)
            return DeliveryStatus.failed, None

        if resp.status_code in (200, 201):
            sid = None
            try:
                sid = resp.json().get("sentiment_id")
            except Exception:
                pass
            status = DeliveryStatus.duplicate if resp.status_code == 200 else DeliveryStatus.sent
            return status, sid
        log.warning("sentiment delivery rejected (%s) for %s", resp.status_code, s.idempotency_key)
        return DeliveryStatus.failed, None

    def close(self) -> None:
        self._client.close()
