"""In-process background job runner for long-running pipeline operations.

The API is served by waitress with a small thread pool, so long-running work
(restart, reprocess, retry) that can take 15-30 minutes must not run inside the
request handler or it holds the HTTP connection open until it finishes. Jobs are
submitted here, run in a daemon thread, and their status is tracked in memory so
callers can poll ``GET /jobs/{id}``.

This is deliberately lightweight (single process, no persistence). If the
container restarts, in-flight job status is lost, but the pipeline itself is
idempotent/resumable, so the work can simply be re-submitted.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("quant_cnbc.jobs")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRegistry:
    """Thread-safe registry that runs callables in background daemon threads."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(
        self, kind: str, target: Callable[[], Any], *, key: str | None = None
    ) -> dict[str, Any]:
        """Start ``target`` in a background thread and return the job record.

        If ``key`` is supplied and a job with the same key is already running,
        that existing job is returned instead of starting a duplicate.
        """
        with self._lock:
            if key is not None:
                for existing in self._jobs.values():
                    if existing["key"] == key and existing["status"] == "running":
                        return existing
            job_id = uuid.uuid4().hex
            job: dict[str, Any] = {
                "id": job_id,
                "kind": kind,
                "key": key,
                "status": "running",
                "submitted_at": _now(),
                "finished_at": None,
                "result": None,
                "error": None,
            }
            self._jobs[job_id] = job

        def _run() -> None:
            try:
                result = target()
                job["result"] = dict(result) if hasattr(result, "items") else result
                job["status"] = "completed"
            except Exception as exc:  # isolate background failures
                log.exception("background job %s (%s) failed", job_id, kind)
                job["error"] = str(exc)[:500]
                job["status"] = "failed"
            finally:
                job["finished_at"] = _now()

        threading.Thread(
            target=_run, name=f"job-{kind}-{job_id[:8]}", daemon=True
        ).start()
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._jobs.values())


registry = JobRegistry()
