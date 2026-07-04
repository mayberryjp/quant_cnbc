"""archive.org TV-CNBC client: discovery, caption selection, download, parsing.

Item identifiers follow ``CNBC_<YYYYMMDD>_<HHMMSS>_<Show_Name>`` and are the
canonical dedup/tracking key. Discovery pages the advancedsearch API by
``addeddate`` ascending so a cursor can resume from the last seen item.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

# Caption filename preference (best first).
_CAPTION_SUFFIXES = (".srt", ".vtt", ".cc5.txt", ".cc1.txt", ".closedcaptions.txt", ".djvu.txt", ".txt")
_TIMECODE = re.compile(r"-->")
_SEQ_ONLY = re.compile(r"^\d+$")
_SRT_TS = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->")


@dataclass
class ArchiveItem:
    identifier: str
    title: str | None
    show_slug: str | None
    air_date: date
    broadcast_start: datetime | None
    added_at: datetime | None
    source_url: str


def parse_identifier(identifier: str) -> tuple[str | None, date | None, datetime | None]:
    """Return (show_slug, air_date, broadcast_start) parsed from an item id."""
    parts = identifier.split("_")
    if len(parts) < 4 or parts[0] != "CNBC":
        return None, None, None
    try:
        d = datetime.strptime(parts[1], "%Y%m%d").date()
    except ValueError:
        return None, None, None
    start = None
    try:
        t = datetime.strptime(parts[2], "%H%M%S").time()
        start = datetime.combine(d, t, tzinfo=timezone.utc)
    except ValueError:
        pass
    show_slug = "_".join(parts[3:]) or None
    return show_slug, d, start


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_caption(text: str) -> str:
    """Strip SRT/VTT sequence numbers + timecodes; collapse to plain text."""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT":
            continue
        if _SEQ_ONLY.match(line) or _TIMECODE.search(line) or _SRT_TS.match(line):
            continue
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pick_caption_file(files: list[dict]) -> str | None:
    """Choose the best caption/transcript filename from a metadata file list."""
    names = [f.get("name", "") for f in files if f.get("name")]
    for suffix in _CAPTION_SUFFIXES:
        for name in names:
            if name.lower().endswith(suffix):
                return name
    return None


class ArchiveClient:
    def __init__(
        self, *, base_url: str = "https://archive.org", collection: str = "TV-CNBC",
        rate_limit: float = 1.0, client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.collection = collection
        self.rate_limit = rate_limit
        self._client = client or httpx.Client(
            timeout=60.0, headers={"User-Agent": "quant_cnbc/0.1 (+https://github.com/mayberryjp/quant_cnbc)"}
        )
        self._last_call = 0.0

    def _throttle(self) -> None:
        if self.rate_limit <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self._last_call = time.monotonic()

    def search(
        self, *, since: datetime | None = None, rows: int = 100, page: int = 1,
    ) -> list[ArchiveItem]:
        """Page the advancedsearch API, oldest-addeddate first (for cursoring)."""
        q = f"collection:{self.collection}"
        if since is not None:
            q += f" AND addeddate:[{since.strftime('%Y-%m-%dT%H:%M:%SZ')} TO *]"
        params = [
            ("q", q), ("fl[]", "identifier"), ("fl[]", "title"),
            ("fl[]", "date"), ("fl[]", "addeddate"),
            ("sort[]", "addeddate asc"), ("rows", str(rows)),
            ("page", str(page)), ("output", "json"),
        ]
        self._throttle()
        resp = self._client.get(f"{self.base_url}/advancedsearch.php", params=params)
        resp.raise_for_status()
        docs = resp.json().get("response", {}).get("docs", [])
        items = (self._to_item(d) for d in docs)
        return [it for it in items if it is not None]

    def _to_item(self, doc: dict) -> ArchiveItem | None:
        identifier = doc.get("identifier")
        if not identifier:
            return None
        show_slug, air_date, broadcast_start = parse_identifier(identifier)
        if air_date is None:
            air_date = (_parse_dt(doc.get("date")) or datetime.now(timezone.utc)).date()
        return ArchiveItem(
            identifier=identifier,
            title=doc.get("title"),
            show_slug=show_slug,
            air_date=air_date,
            broadcast_start=broadcast_start,
            added_at=_parse_dt(doc.get("addeddate")),
            source_url=f"{self.base_url}/details/{identifier}",
        )

    def list_files(self, identifier: str) -> list[dict]:
        self._throttle()
        resp = self._client.get(f"{self.base_url}/metadata/{identifier}")
        resp.raise_for_status()
        return resp.json().get("files", [])

    def download_file(self, identifier: str, filename: str) -> str:
        self._throttle()
        resp = self._client.get(f"{self.base_url}/download/{identifier}/{filename}")
        resp.raise_for_status()
        return resp.text

    def close(self) -> None:
        self._client.close()
