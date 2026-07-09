"""LLM pass 1 - distillation. Transcript text -> structured summary JSON."""

from __future__ import annotations

import logging
from typing import Any

from app.models.llm_schemas import DistillOutput

log = logging.getLogger("quant_cnbc.distiller")

_DEPTH = (
    "Be EXHAUSTIVE. Cover EVERY distinct topic, company, ticker, guest, trade, and market "
    "discussed — do not omit any segment, and do not merge unrelated points into one line. "
    "Favor depth and breadth over brevity: the summary should be long and comprehensive, with a "
    "dedicated section for each topic and a sub-bullet for each specific point within it. "
    "Preserve concrete specifics wherever the source states them: tickers, company names, price "
    "levels, percentage moves, price targets, earnings and guidance numbers, analyst ratings and "
    "upgrades/downgrades, deals (M&A, partnerships, debt/equity raises), macro data, and which "
    "speaker or guest made each call. Do NOT shorten, generalize, or drop details to save space. "
    "Capture all key points accurately and do not invent information that is not present in the source."
)

_SUMMARY_FORMAT = (
    "The \"summary\" value MUST be a Markdown document with this structure:\n"
    "- A bold title line naming the show and date if known, e.g. "
    "\"**CNBC Fast Money Transcript Summary (June 24, 2026)**\".\n"
    "- A numbered list of the major topics in the order discussed; each item starts with a bold "
    "section heading (e.g. \"1. **Market Overview**:\", \"2. **Micron Technology**:\").\n"
    "- Under each heading, an indented Markdown bullet list where every distinct sub-point is its "
    "own bullet beginning with a bold label and a colon (e.g. \"   - **Earnings**: ...\").\n"
    "- End with a single closing sentence stating what the summary captures.\n"
    + _DEPTH
)

DISTILL_SYSTEM = (
    "Summarize the following document into a thorough, self-contained, DETAILED summary. "
    + _SUMMARY_FORMAT
    + " Return ONLY a JSON object with keys: "
    '"summary" (the Markdown summary described above), '
    '"key_topics" (array of short strings — one per numbered section/topic), and '
    '"segments" (array of objects with "speaker", "role", and "summary").'
)

_REDUCE_SYSTEM = (
    "The following are DETAILED summaries of consecutive parts of ONE document. Merge them into a "
    "single summary that RETAINS ALL detail from every part — combine overlapping topics and drop "
    "only exact duplicates, but keep every distinct topic, company, ticker, number, rating, deal, "
    "trade, and named speaker that appears in ANY part. This is a merge, NOT a re-summarization: do "
    "not compress or shorten. The result must be at least as long and detailed as the parts combined. "
    "Order sections as the document progressed. "
    + _SUMMARY_FORMAT
    + ' Return ONLY the same JSON object shape ("summary", "key_topics", "segments").'
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
        log.info("distill: single-shot over %d chars", len(text))
        data, usage = llm_client.complete_json(DISTILL_SYSTEM, _user_prompt(text))
        return DistillOutput.model_validate(data), usage

    # Map: summarize each chunk, then reduce the concatenation.
    chunks = _chunks(text, max_chunk_chars)
    log.info("distill: map/reduce over %d chunks (%d chars total)", len(chunks), len(text))
    partials: list[str] = []
    total_usage: dict[str, Any] = {}
    for idx, chunk in enumerate(chunks, 1):
        log.info("distill: mapping chunk %d/%d (%d chars)", idx, len(chunks), len(chunk))
        data, usage = llm_client.complete_json(DISTILL_SYSTEM, _user_prompt(chunk))
        partials.append(DistillOutput.model_validate(data).summary)
        _merge_usage(total_usage, usage)

    log.info("distill: reducing %d partial summaries", len(partials))
    combined = "\n\n".join(f"- {p}" for p in partials)
    data, usage = llm_client.complete_json(_REDUCE_SYSTEM, _user_prompt(combined))
    _merge_usage(total_usage, usage)
    return DistillOutput.model_validate(data), total_usage


def _merge_usage(acc: dict[str, Any], usage: dict[str, Any]) -> None:
    for k, v in (usage or {}).items():
        if isinstance(v, (int, float)):
            acc[k] = acc.get(k, 0) + v
