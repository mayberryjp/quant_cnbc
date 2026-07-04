"""Health, readiness, and operational visibility routes."""

from __future__ import annotations

from bottle import Bottle

from app import db

sub = Bottle()


@sub.get("/cnbc/health")
def health():
    """Liveness — does not depend on the database."""
    return {"status": "ok"}


@sub.get("/cnbc/ready")
def readiness():
    """Readiness — database reachable + freshness of the last ingest run."""
    db_ok = db.ping()
    heartbeat = None
    last_run_status = None
    if db_ok:
        try:
            from app.repository.runs import RunRepository

            repo = RunRepository(db.get_engine())
            latest = repo.latest_run()
            if latest:
                heartbeat = latest.get("heartbeat_at")
                last_run_status = latest.get("status")
        except Exception:
            # Migrations may not have run yet; readiness still reports db state.
            pass
    return {
        "status": "ready" if db_ok else "not_ready",
        "database": "ok" if db_ok else "unavailable",
        "last_run_status": last_run_status,
        "heartbeat": str(heartbeat) if heartbeat else None,
    }


@sub.get("/cnbc/stats")
def stats():
    """Operational counters + last run summary."""
    from app.repository.runs import RunRepository

    repo = RunRepository(db.get_engine())
    return repo.stats()
