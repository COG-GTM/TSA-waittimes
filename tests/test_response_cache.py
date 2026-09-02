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
    assert response.headers["cache-control"] == "public, max-age=30"


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

    first = await main.api_leaderboard()
    second = await main.api_leaderboard()

    assert calls == 1
    assert first.headers["cache-control"] == "public, max-age=30"
    assert second.headers["cache-control"] in {"public, max-age=29", "public, max-age=30"}


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

    first = await main.api_airport_typical("SEA", request)
    second = await main.api_airport_typical("SEA", request)

    assert calls == 1
    assert first.headers["cache-control"] == "public, max-age=30"
    assert second.headers["cache-control"] in {"public, max-age=29", "public, max-age=30"}


@pytest.mark.asyncio
async def test_near_expiry_cache_headers_reflect_remaining_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(seconds=5, microseconds=900_000)
    payload = {"generated_at": "cached"}
    main.SUMMARY_CACHE.clear()
    main.LEADERBOARD_CACHE.clear()
    main.TYPICAL_CACHE.clear()
    main.SUMMARY_CACHE["global"] = (expires_at, payload)
    main.LEADERBOARD_CACHE["global"] = (expires_at, payload)
    main.TYPICAL_CACHE["SEA"] = (expires_at, payload)

    async def unexpected_summary() -> dict[str, Any]:
        raise AssertionError("summary cache miss")

    async def unexpected_leaderboard(_now: datetime) -> dict[str, Any]:
        raise AssertionError("leaderboard cache miss")

    async def unexpected_typical(_iata: str) -> dict[str, Any]:
        raise AssertionError("typical cache miss")

    monkeypatch.setattr(main, "_summary_snapshot", unexpected_summary)
    monkeypatch.setattr(main, "_leaderboard_snapshot", unexpected_leaderboard)
    monkeypatch.setattr(main, "_typical_snapshot", unexpected_typical)
    request = request_for("/api/airport/SEA/typical")

    summary = await main.api_summary()
    leaderboard = await main.api_leaderboard()
    typical = await main.api_airport_typical("SEA", request)

    assert summary.headers["cache-control"] == "public, max-age=5"
    assert leaderboard.headers["cache-control"] == "public, max-age=5"
    assert typical.headers["cache-control"] == "public, max-age=5"
