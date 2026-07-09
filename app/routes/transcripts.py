"""Transcript read API + reprocess/run-trigger endpoints."""

from __future__ import annotations

import json
import logging

from bottle import Bottle, HTTPResponse, request, response
from pydantic import ValidationError

from app import dependencies as deps
from app.config import settings
from app.models.requests import ReprocessRequest, RetryFailedRequest, RunTriggerRequest

from app.models.responses import (
    DistillationResponse,
    TranscriptDetailResponse,
    TranscriptResponse,
)

log = logging.getLogger("quant_cnbc.routes.transcripts")
sub = Bottle()


def _json_error(status: int, detail) -> HTTPResponse:
    return HTTPResponse(
        status=status, body=json.dumps({"detail": detail}),
        content_type="application/json",
    )


def _page_params() -> tuple[int, int]:
    page = max(1, int(request.params.get("page", 1)))
    page_size = min(int(request.params.get("page_size", settings.default_page_size)),
                    settings.max_page_size)
    return page, page_size


@sub.get("/transcripts")
def list_transcripts():
    page, page_size = _page_params()
    repo = deps.transcript_repo()
    items, total = repo.list(
        status=request.params.get("status"),
        show=request.params.get("show"),
        from_date=request.params.get("from_date"),
        to_date=request.params.get("to_date"),
        page=page, page_size=page_size,
    )
    return {
        "items": [TranscriptResponse(**t.model_dump()).model_dump(mode="json") for t in items],
        "total": total, "page": page, "page_size": page_size,
    }


@sub.get("/transcripts/<transcript_id:int>")
def get_transcript(transcript_id: int):
    repo = deps.transcript_repo()
    t = repo.get_by_id(transcript_id)
    if t is None:
        raise _json_error(404, "Transcript not found")
    current = deps.distillation_repo().get_current(transcript_id)
    detail = TranscriptDetailResponse(
        **t.model_dump(),
        distillation=(DistillationResponse(**current.model_dump()) if current else None),
    )
    return detail.model_dump(mode="json")


@sub.post("/transcripts/<archive_identifier:path>/reprocess")
def reprocess_one(archive_identifier: str):
    from app.services.ingest_worker import build_pipeline

    pipeline = build_pipeline()
    t = pipeline.transcripts.get_by_identifier(archive_identifier)
    if t is None:
        raise _json_error(404, "Transcript not found")
    totals = pipeline.reprocess(t)
    response.status = 202
    return {"status": "reprocessed", "archive_identifier": archive_identifier, "counters": dict(totals)}


@sub.post("/reprocess")
def reprocess_bulk():
    try:
        body = ReprocessRequest(**(request.json or {}))
    except ValidationError as e:
        raise _json_error(422, json.loads(e.json()))
    from app.services.ingest_worker import build_pipeline

    pipeline = build_pipeline()
    candidates = pipeline.transcripts.reprocess_candidates(
        show=body.show, from_date=body.from_date, to_date=body.to_date,
        only_stale=body.only_stale, current_model=settings.llm_model,
        current_prompt=settings.distill_prompt_version,
    )
    for t in candidates:
        pipeline.reprocess(t)
    response.status = 202
    return {
        "status": "reprocessing",
        "matched": len(candidates),
        "archive_identifiers": [t.archive_identifier for t in candidates],
    }


@sub.post("/runs/trigger")
def trigger_run():
    try:
        body = RunTriggerRequest(**(request.json or {}))
    except ValidationError as e:
        raise _json_error(422, json.loads(e.json()))
    from app.services.ingest_worker import build_pipeline

    totals = build_pipeline().run(body.run_date)
    response.status = 202
    return {"status": "completed", "counters": dict(totals)}


@sub.post("/retry-failed")
def retry_failed():
    try:
        body = RetryFailedRequest(**(request.json or {}))
    except ValidationError as e:
        raise _json_error(422, json.loads(e.json()))
    from app.services.ingest_worker import build_pipeline

    totals = build_pipeline().retry_failed(
        show=body.show, from_date=body.from_date, to_date=body.to_date,
        max_attempts=body.max_attempts,
    )
    response.status = 202
    return {"status": "retried", "counters": dict(totals)}
