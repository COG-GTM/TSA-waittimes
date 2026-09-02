import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import Request

from app import main


def request_for(path: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 1234),
    })


@pytest.mark.asyncio
async def test_summary_cache_avoids_requery_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    payload = {"generated_at": "2026-01-01T00:00:00+00:00"}

    async def snapshot() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return payload

    main.SUMMARY_CACHE.clear()
    monkeypatch.setattr(main, "_summary_snapshot", snapshot)

    first = await main.api_summary()
    second = await main.api_summary()

    assert calls == 1
    assert json.loads(first.body) == json.loads(second.body) == payload
    assert first.headers["cache-control"] == "public, max-age=30"


@pytest.mark.asyncio
async def test_expired_summary_cache_recomputes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    payload = {"generated_at": "2026-01-01T00:00:00+00:00"}

    async def snapshot() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return payload

    main.SUMMARY_CACHE.clear()
    main.SUMMARY_CACHE["global"] = (
        datetime.now(UTC) - timedelta(seconds=1),
        {"generated_at": "expired"},
    )
    monkeypatch.setattr(main, "_summary_snapshot", snapshot)

    response = await main.api_summary()

    assert calls == 1
    assert json.loads(response.body) == payload


@pytest.mark.asyncio
async def test_leaderboard_cache_avoids_requery_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    payload = {"generated_at": "2026-01-01T00:00:00+00:00"}

    async def snapshot(_now: datetime) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return payload

    main.LEADERBOARD_CACHE.clear()
    monkeypatch.setattr(main, "_leaderboard_snapshot", snapshot)

    await main.api_leaderboard()
    await main.api_leaderboard()

    assert calls == 1


@pytest.mark.asyncio
async def test_typical_cache_avoids_requery_per_airport(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    payload = {"airport": {"iata": "SEA"}}

    async def snapshot(_iata: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return payload

    main.TYPICAL_CACHE.clear()
    monkeypatch.setattr(main, "_typical_snapshot", snapshot)
    request = request_for("/api/airport/SEA/typical")

    await main.api_airport_typical("SEA", request)
    await main.api_airport_typical("SEA", request)

    assert calls == 1
