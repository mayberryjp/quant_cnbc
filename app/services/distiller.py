"""LLM pass 1 - distillation. Transcript text -> structured summary JSON."""

from __future__ import annotations

import logging
from typing import Any

from app.models.llm_schemas import DistillOutput

log = logging.getLogger("quant_cnbc.distiller")

DISTILL_SYSTEM = (
    "You are a financial-news analyst. You distill CNBC show transcripts into a "
    "concise, faithful structured summary. Return ONLY a JSON object with keys: "
    '"summary" (string, 3-6 sentences of the key market/company points), '
    '"key_topics" (array of short strings), and '
    '"segments" (array of objects with "speaker", "role", and "summary"). '
    "Do not invent facts that are not in the transcript."
)

_REDUCE_SYSTEM = (
    "You are combining several partial summaries of one CNBC show into a single "
    "structured summary. Return ONLY the same JSON object shape "
    '("summary", "key_topics", "segments").'
)


def _user_prompt(text: str) -> str:
    return f"Transcript:\n\"\"\"\n{text}\n\"\"\"\n\nReturn the JSON object."


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def distill(
    llm_client, text: str, *, max_chunk_chars: int = 12000,
) -> tuple[DistillOutput, dict[str, Any]]:
    """Distill transcript text. Long transcripts are map/reduced.

    Returns (DistillOutput, aggregated token usage).
    """
    if len(text) <= max_chunk_chars:
        data, usage = llm_client.complete_json(DISTILL_SYSTEM, _user_prompt(text))
        return DistillOutput.model_validate(data), usage

    # Map: summarize each chunk, then reduce the concatenation.
    partials: list[str] = []
    total_usage: dict[str, Any] = {}
    for chunk in _chunks(text, max_chunk_chars):
        data, usage = llm_client.complete_json(DISTILL_SYSTEM, _user_prompt(chunk))
        partials.append(DistillOutput.model_validate(data).summary)
        _merge_usage(total_usage, usage)

    combined = "\n\n".join(f"- {p}" for p in partials)
    data, usage = llm_client.complete_json(_REDUCE_SYSTEM, _user_prompt(combined))
    _merge_usage(total_usage, usage)
    return DistillOutput.model_validate(data), total_usage


def _merge_usage(acc: dict[str, Any], usage: dict[str, Any]) -> None:
    for k, v in (usage or {}).items():
        if isinstance(v, (int, float)):
            acc[k] = acc.get(k, 0) + v
