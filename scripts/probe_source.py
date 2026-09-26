#!/usr/bin/env python3
"""Run one live fetch for a source, print the parsed observations, and record a fixture.

    python scripts/probe_source.py SEA            # fetch, print, write tests/fixtures/sea.json
    python scripts/probe_source.py SEA --no-save  # fetch and print only

The recorded fixture holds every HTTP exchange the adapter performed plus the
observations it parsed, so `pytest` can replay it offline.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sources.adapters import SOURCES
from app.sources.base import USER_AGENT, FetchResult, Source
from app.sources.credentials import FEED_CREDENTIALS
from tests.harness import (
    FIXTURE_VERSION,
    fixture_path,
    observation_to_dict,
    redact_credentials,
)

CODE_RE = re.compile(r"^[A-Z]{3}$")


class RecordingTransport(httpx.AsyncHTTPTransport):
    """Performs real requests while recording each request/response pair."""

    def __init__(self) -> None:
        super().__init__()
        self.exchanges: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        await response.aread()
        exchange: dict[str, Any] = {
            "method": request.method,
            "url": str(request.url),
            "status": response.status_code,
            "content_type": response.headers.get("content-type", "application/json"),
        }
        try:
            exchange["json"] = json.loads(response.text)
        except ValueError:
            exchange["text"] = response.text
        self.exchanges.append(exchange)
        return response


def find_source(code: str) -> Source:
    for source in SOURCES:
        if source.code == code:
            return source
    known = ", ".join(sorted(s.code for s in SOURCES))
    raise SystemExit(f"unknown source '{code}'; known sources: {known}")


async def probe(source: Source) -> tuple[FetchResult, list[dict[str, Any]]]:
    transport = RecordingTransport()
    async with httpx.AsyncClient(
        transport=transport,
        headers={"User-Agent": USER_AGENT},
        timeout=30,
        follow_redirects=True,
    ) as client:
        result = await source.fetch(client)
    return result, transport.exchanges


def summarize(observations: list[dict[str, Any]]) -> dict[str, Any]:
    lane_types: dict[str, int] = {}
    for ob in observations:
        lane_types[ob["lane_type"]] = lane_types.get(ob["lane_type"], 0) + 1
    waits = [ob["wait_seconds"] for ob in observations if ob["wait_seconds"] is not None]
    return {
        "observation_count": len(observations),
        "lane_types": dict(sorted(lane_types.items())),
        "open_count": sum(1 for ob in observations if ob["is_open"]),
        "closed_count": sum(1 for ob in observations if not ob["is_open"]),
        "with_published_at": sum(1 for ob in observations if ob["published_at"]),
        "null_wait_count": len(observations) - len(waits),
        "max_wait_seconds": max(waits) if waits else None,
        "observations": observations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("code", help="airport IATA code of a source in app/sources/adapters.py, e.g. SEA")
    parser.add_argument("--no-save", action="store_true", help="print only; do not write the fixture")
    args = parser.parse_args()

    code = args.code.strip().upper()
    if not CODE_RE.match(code):
        raise SystemExit("code must be a three-letter IATA code")
    source = find_source(code)

    print(f"{source.code}: {source.name}\n  public page: {source.url}\n  attribution: {source.attribution}")
    result, exchanges = asyncio.run(probe(source))

    observations = [observation_to_dict(ob) for ob in result.observations]
    for ob in observations:
        wait = ob["wait_seconds"]
        wait_text = "n/a" if wait is None else f"{wait}s ({wait / 60:.0f} min)"
        state = "open" if ob["is_open"] else "CLOSED"
        print(f"  - {ob['checkpoint_name']} [{ob['lane_type']}] {wait_text} {state} {ob['published_at'] or ''}")
    print(f"  {len(observations)} observation(s) from {len(exchanges)} request(s)")
    if not observations:
        print("  WARNING: empty result — an empty successful fetch is not a healthy poll")

    if args.no_save:
        return 0
    fixture = {
        "version": FIXTURE_VERSION,
        "source": source.code,
        "captured_at": datetime.now(tz=UTC).isoformat(),
        "public_page": source.url,
        "attribution": source.attribution,
        "exchanges": redact_credentials(
            exchanges, {name: os.environ.get(name, "").strip() for name in FEED_CREDENTIALS}
        ),
        "expected": summarize(observations),
    }
    path = fixture_path(source.code)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(fixture, fh, indent=2, sort_keys=False)
        fh.write("\n")
    print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
