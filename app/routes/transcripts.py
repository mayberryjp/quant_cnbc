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
    summaries = deps.distillation_repo().get_current_map([t.id for t in items])
    return {
        "items": [
            TranscriptResponse(
                **t.model_dump(),
                summary=(summaries[t.id].summary if t.id in summaries else None),
            ).model_dump(mode="json")
            for t in items
        ],
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
        summary=(current.summary if current else None),
        distillation=(DistillationResponse(**current.model_dump()) if current else None),
    )
    return detail.model_dump(mode="json")


@sub.delete("/transcripts/<transcript_id:int>")
def delete_transcript(transcript_id: int):
    repo = deps.transcript_repo()
    if not repo.delete(transcript_id):
        raise _json_error(404, "Transcript not found")
    return {"status": "deleted", "id": transcript_id}


@sub.post("/transcripts/<archive_identifier:path>/reprocess")
def reprocess_one(archive_identifier: str):
    from app.services.ingest_worker import build_pipeline
    from app.services.jobs import registry

    pipeline = build_pipeline()
    t = pipeline.transcripts.get_by_identifier(archive_identifier)
    if t is None:
        raise _json_error(404, "Transcript not found")

    # Re-distilling and re-delivering can take several minutes; run it in the
    # background so the request returns immediately.
    job = registry.submit(
        "reprocess", lambda: pipeline.reprocess(t), key=f"reprocess:{archive_identifier}"
    )
    response.status = 202
    return {
        "status": "accepted",
        "job_id": job["id"],
        "job_status": job["status"],
        "archive_identifier": archive_identifier,
    }


@sub.post("/transcripts/<archive_identifier:path>/restart")
def restart_one(archive_identifier: str):
    from app.services.ingest_worker import build_pipeline
    from app.services.jobs import registry

    pipeline = build_pipeline()
    t = pipeline.transcripts.get_by_identifier(archive_identifier)
    if t is None:
        raise _json_error(404, "Transcript not found")

    # A full restart re-fetches and re-runs every pass (15-30 min); run it in the
    # background so the request returns immediately instead of holding the
    # connection open until the job finishes.
    job = registry.submit(
        "restart", lambda: pipeline.restart(t), key=f"restart:{archive_identifier}"
    )
    response.status = 202
    return {
        "status": "accepted",
        "job_id": job["id"],
        "job_status": job["status"],
        "archive_identifier": archive_identifier,
    }


@sub.get("/jobs/<job_id>")
def get_job(job_id: str):
    from app.services.jobs import registry

    job = registry.get(job_id)
    if job is None:
        raise _json_error(404, "Job not found")
    return job


@sub.post("/reprocess")
def reprocess_bulk():
    try:
        body = ReprocessRequest(**(request.json or {}))
    except ValidationError as e:
        raise _json_error(422, json.loads(e.json()))
    from app.services.ingest_worker import build_pipeline
    from app.services.jobs import registry

    pipeline = build_pipeline()
    candidates = pipeline.transcripts.reprocess_candidates(
        show=body.show, from_date=body.from_date, to_date=body.to_date,
        only_stale=body.only_stale, current_model=settings.llm_model,
        current_prompt=settings.distill_prompt_version,
    )

    def _job():
        for t in candidates:
            pipeline.reprocess(t)
        return {"reprocessed": len(candidates)}

    job = registry.submit("reprocess-bulk", _job)
    response.status = 202
    return {
        "status": "accepted",
        "job_id": job["id"],
        "job_status": job["status"],
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
    from app.services.jobs import registry

    run_date = body.run_date
    job = registry.submit("run", lambda: build_pipeline().run(run_date))
    response.status = 202
    return {"status": "accepted", "job_id": job["id"], "job_status": job["status"]}


@sub.post("/retry-failed")
def retry_failed():
    try:
        body = RetryFailedRequest(**(request.json or {}))
    except ValidationError as e:
        raise _json_error(422, json.loads(e.json()))
    from app.services.ingest_worker import build_pipeline
    from app.services.jobs import registry

    def _job():
        kwargs = {
            "show": body.show,
            "from_date": body.from_date,
            "to_date": body.to_date,
            "max_attempts": body.max_attempts,
        }
        if body.delete_after_attempts is not None:
            kwargs["delete_after_attempts"] = body.delete_after_attempts
        return build_pipeline().retry_failed(
            **kwargs,
        )

    job = registry.submit("retry-failed", _job)
    response.status = 202
    return {"status": "accepted", "job_id": job["id"], "job_status": job["status"]}
