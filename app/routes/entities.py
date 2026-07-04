"""Read API for referenced entities and sentiments."""

from __future__ import annotations

from bottle import Bottle, request

from app import dependencies as deps
from app.config import settings
from app.models.responses import EntityResponse, SentimentResponse

sub = Bottle()


def _page_params() -> tuple[int, int]:
    page = max(1, int(request.params.get("page", 1)))
    page_size = min(int(request.params.get("page_size", settings.default_page_size)),
                    settings.max_page_size)
    return page, page_size


@sub.get("/entities")
def list_entities():
    page, page_size = _page_params()
    repo = deps.entity_repo()
    items, total = repo.list(
        ticker=request.params.get("ticker"),
        status=request.params.get("status"),
        page=page, page_size=page_size,
    )
    return {
        "items": [EntityResponse(**e.model_dump()).model_dump(mode="json") for e in items],
        "total": total, "page": page, "page_size": page_size,
    }


@sub.get("/sentiments")
def list_sentiments():
    page, page_size = _page_params()
    repo = deps.sentiment_repo()
    items, total = repo.list(
        subject=request.params.get("subject"),
        status=request.params.get("status"),
        page=page, page_size=page_size,
    )
    return {
        "items": [SentimentResponse(**s.model_dump()).model_dump(mode="json") for s in items],
        "total": total, "page": page, "page_size": page_size,
    }
