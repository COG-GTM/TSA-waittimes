"""Fixture-replay harness for source adapters.

A fixture records every HTTP exchange one live fetch performed, plus the
observations the adapter parsed out of them. Tests replay the recorded
responses through the adapter with a mock transport, so they never touch the
network.

Adding coverage for a new adapter is only:

    python scripts/probe_source.py <CODE>

which writes ``tests/fixtures/<code>.json``; the replay tests pick it up
automatically.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from app.sources.base import USER_AGENT, FetchResult, Observation, Source

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE_VERSION = 1


def fixture_path(code: str) -> Path:
    return FIXTURE_DIR / f"{code.lower()}.json"


def load_fixture(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        fixture = json.load(fh)
    for key in ("version", "source", "exchanges", "expected"):
        if key not in fixture:
            raise ValueError(f"{path.name}: missing '{key}'")
    if fixture["version"] != FIXTURE_VERSION:
        raise ValueError(f"{path.name}: unsupported fixture version {fixture['version']}")
    return fixture


def observation_to_dict(ob: Observation) -> dict[str, Any]:
    data = asdict(ob)
    data["published_at"] = ob.published_at.isoformat() if ob.published_at else None
    return data


def _response(exchange: dict[str, Any]) -> httpx.Response:
    headers = {"Content-Type": exchange.get("content_type", "application/json")}
    if "json" in exchange:
        return httpx.Response(exchange["status"], json=exchange["json"], headers=headers)
    return httpx.Response(exchange["status"], text=exchange.get("text", ""), headers=headers)


def replay_transport(exchanges: list[dict[str, Any]]) -> httpx.MockTransport:
    """A transport that answers only the exact requests the fixture recorded."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for exchange in exchanges:
        by_key.setdefault((exchange["method"], exchange["url"]), []).append(exchange)

    def handler(request: httpx.Request) -> httpx.Response:
        queued = by_key.get((request.method, str(request.url)))
        if not queued:
            raise AssertionError(f"unrecorded request: {request.method} {request.url}")
        return _response(queued[0] if len(queued) == 1 else queued.pop(0))

    return httpx.MockTransport(handler)


async def replay(source: Source, fixture: dict[str, Any]) -> FetchResult:
    """Run an adapter against its recorded payloads, with no network access."""
    async with httpx.AsyncClient(
        transport=replay_transport(fixture["exchanges"]),
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    ) as client:
        return await source.fetch(client)
