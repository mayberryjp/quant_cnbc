"""LLM pass 3 - entities. Distillation -> referenced tickers/companies -> watchlist.

Every ticker or company referenced is submitted to the quant_signals watchlist;
the LLM resolves company names to tickers in the same pass. Mentions that cannot
be resolved to a ticker are held locally as ``unresolved`` (not submitted).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.models.domain import Direction, ReferencedEntity, Transcript, WatchlistStatus
from app.models.llm_schemas import EntityOutput
from app.services.http_util import request_with_retry

log = logging.getLogger("quant_cnbc.entities")

ENTITY_SYSTEM = (
    "You extract every company or ticker referenced in a distilled CNBC show "
    "summary. Return ONLY a JSON object: "
    '{"entities": [{"raw_mention": "as said", '
    '"entity_type": "ticker|company", "company_name": "normalized name", '
    '"ticker": "RESOLVED_TICKER or null", "speaker": "who mentioned it or null", '
    '"direction": "long|short|neutral or null", "confidence": 0.0..1.0, '
    '"context": "short quote or rationale"}]}. '
    "Resolve company names to their US stock ticker where possible; if you cannot "
    "confidently resolve a ticker, set ticker to null."
)


def idempotency_key(archive_identifier: str, key: str, model: str, prompt_version: str) -> str:
    return f"cnbc:{archive_identifier}:{key}:{model}:{prompt_version}"


def extract_entities(llm_client, distill_summary: str) -> tuple[EntityOutput, dict[str, Any]]:
    data, usage = llm_client.complete_json(
        ENTITY_SYSTEM, f"Distilled summary:\n{distill_summary}\n\nReturn the JSON object."
    )
    out = EntityOutput.model_validate(data)
    resolved = sum(1 for e in out.entities if e.ticker)
    log.info(
        "entities: extracted %d mention(s), %d resolved to a ticker",
        len(out.entities), resolved,
    )
    return out, usage


def build_rows(
    transcript: Transcript, output: EntityOutput, *, model: str, prompt_version: str,
) -> list[ReferencedEntity]:
    rows: list[ReferencedEntity] = []
    seen: set[str] = set()
    for ent in output.entities:
        key = ent.ticker or ent.raw_mention
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            ReferencedEntity(
                transcript_id=transcript.id,
                raw_mention=ent.raw_mention,
                entity_type=ent.entity_type,
                company_name=ent.company_name,
                ticker=ent.ticker,
                speaker=ent.speaker,
                direction=ent.direction,
                confidence=ent.confidence,
                context=ent.context,
                model=model,
                prompt_version=prompt_version,
                idempotency_key=idempotency_key(
                    transcript.archive_identifier, key, model, prompt_version
                ),
                watchlist_status=(
                    WatchlistStatus.pending if ent.ticker else WatchlistStatus.unresolved
                ),
            )
        )
    return rows


class WatchlistApiClient:
    """Submits resolved entities to the quant_signals ``POST /signals`` API."""

    def __init__(
        self, *, url: str, api_key: str = "", source: str = "cnbc",
        signal_type: str = "cnbc_mention", timeout: int = 30, retries: int = 3,
        backoff: float = 1.0, client: httpx.Client | None = None,
    ) -> None:
        self.url = url
        self.source = source
        self.signal_type = signal_type
        self.retries = retries
        self.backoff = backoff
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.Client(timeout=timeout, headers=headers)

    def submit(self, e: ReferencedEntity, transcript: Transcript) -> WatchlistStatus:
        if not e.ticker:
            return WatchlistStatus.unresolved
        body = {
            "source": self.source,
            "idempotency_key": e.idempotency_key,
            "ticker": e.ticker,
            "signal_type": self.signal_type,
            "direction": e.direction.value if isinstance(e.direction, Direction) else e.direction,
            "confidence": e.confidence,
            "reason": e.context or "",
            "tags": ["cnbc", transcript.show_slug] if transcript.show_slug else ["cnbc"],
            "metadata": {
                "show": transcript.show_slug,
                "air_date": str(transcript.air_date),
                "archive_identifier": transcript.archive_identifier,
                "guest": e.speaker,
                "company_name": e.company_name,
                "raw_mention": e.raw_mention,
            },
        }
        try:
            resp = request_with_retry(
                lambda: self._client.post(self.url, json=body),
                retries=self.retries, backoff=self.backoff,
            )
        except httpx.HTTPError as exc:
            log.warning("watchlist submit transport error for %s: %s", e.idempotency_key, exc)
            return WatchlistStatus.failed

        if resp.status_code == 201:
            return WatchlistStatus.submitted
        if resp.status_code == 200:
            return WatchlistStatus.duplicate
        log.warning("watchlist submit rejected (%s) for %s", resp.status_code, e.idempotency_key)
        return WatchlistStatus.failed

    def close(self) -> None:
        self._client.close()
