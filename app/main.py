"""Bottle application entry point (served via waitress)."""

from __future__ import annotations

import logging
import sys

from bottle import Bottle

from app.config import settings
from app.routes import health

SERVICE_NAME = "quant-cnbc-api"
log = logging.getLogger(SERVICE_NAME)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
    force=True,
)

app = Bottle()
app.merge(health.sub)

# Read + reprocess routes are merged as they are implemented.
try:  # pragma: no cover - optional until the slice lands
    from app.routes import transcripts

    app.merge(transcripts.sub)
except ImportError:
    pass

try:  # pragma: no cover - optional until the slice lands
    from app.routes import entities

    app.merge(entities.sub)
except ImportError:
    pass


if __name__ == "__main__":
    from waitress import serve

    log.info(
        "Starting quant_cnbc API on %s:%d ...",
        settings.api_listen_address,
        settings.api_port,
    )
    serve(app, host=settings.api_listen_address, port=settings.api_port, threads=8)
