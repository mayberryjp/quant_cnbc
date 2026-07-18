"""API request schemas."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunTriggerRequest(BaseModel):
    """Body of POST /runs/trigger."""

    model_config = ConfigDict(populate_by_name=True)
    run_date: date | None = Field(default=None, alias="date")


class ReprocessRequest(BaseModel):
    """Body of POST /reprocess (bulk recalculation by filter)."""

    model_config = ConfigDict(populate_by_name=True)
    show: str | None = None
    from_date: date | None = None
    to_date: date | None = None
    only_stale: bool = False
    # Which pass to start from; `from` is reserved in Python, exposed via alias.
    from_stage: Literal["distill", "sentiment", "entities"] | None = Field(
        default=None, alias="from"
    )


class RetryFailedRequest(BaseModel):
    """Body of POST /retry-failed (re-run items in the 'failed' state)."""

    model_config = ConfigDict(populate_by_name=True)
    show: str | None = None
    from_date: date | None = None
    to_date: date | None = None
    # Only retry rows whose attempt count is still below this ceiling.
    max_attempts: int | None = None
    # Delete rows that have reached this many failed attempts.
    delete_after_attempts: int | None = None

