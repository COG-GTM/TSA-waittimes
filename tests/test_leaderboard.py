"""Tests for the national waits leaderboard."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import pytest

from app import leaderboard, main

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def row(
    iata: str,
    wait_seconds: int | None,
    *,
    lane: str = "standard",
    age_minutes: int = 1,
    is_open: bool = True,
    checkpoint: str = "Checkpoint",
    published: datetime | None = None,
    reference: datetime = NOW,
) -> tuple[Any, ...]:
    fetched_at = reference - timedelta(minutes=age_minutes)
    published_at = published or reference
    return (
        iata,
        checkpoint,
        lane,
        wait_seconds,
        is_open,
        published_at,
        fetched_at,
        "Airport feed",
        "https://example.test/feed",
    )


def test_ranking_lane_max_and_exclusions() -> None:
    latest = [
        row("AAA", 600),
        row("AAA", 1200),
        row("BBB", 1200),
        row("CCC", 900),
        row("DDD", 600, is_open=False),
        row("EEE", None),
        row("FFF", 300, lane="precheck"),
        row("GGG", 480, lane="other"),
        row("UNKNOWN", 1800),
    ]
    result = leaderboard.build(
        latest,
        [],
        {"AAA": "Airport AAA", "BBB": "Airport BBB", "CCC": "Airport CCC", "DDD": "Airport DDD",
         "EEE": "Airport EEE", "FFF": "Airport FFF", "GGG": "Airport GGG"},
        now=NOW,
    )

    assert [entry["iata"] for entry in result["worst_standard"]["entries"]] == ["AAA", "BBB", "CCC"]
    assert result["worst_standard"]["entries"][0]["wait_seconds"] == 1200
    assert [entry["iata"] for entry in result["worst_precheck"]["entries"]] == ["FFF"]
    assert all(entry["iata"] != "DDD" for entry in result["worst_standard"]["entries"])
    assert all(entry["wait_seconds"] != 0 for entry in result["worst_standard"]["entries"])


def test_ranking_top_ten_and_iata_tie_break() -> None:
    airports = {f"A{i:02d}": f"Airport {i:02d}" for i in range(12)}
    latest = [row(iata, (12 - index) * 60) for index, iata in enumerate(airports)]
    latest[0] = row("A00", 660)
    latest[1] = row("A01", 660)
    result = leaderboard.build(latest, [], airports, now=NOW)

    entries = result["worst_standard"]["entries"]
    assert len(entries) == leaderboard.TOP_N
    assert [entry["iata"] for entry in entries[:2]] == ["A00", "A01"]
    assert entries[-1]["iata"] == "A09"


def test_freshness_cutoff_and_quiet_flag() -> None:
    latest = [
        row("OLD", 900, age_minutes=45),
        row("FRESH", 600, age_minutes=29),
        row("SECOND", 540),
    ]
    result = leaderboard.build(
        latest,
        [],
        {"OLD": "Old", "FRESH": "Fresh", "SECOND": "Second"},
        now=NOW,
    )

    assert [entry["iata"] for entry in result["worst_standard"]["entries"]] == ["FRESH", "SECOND"]
    assert result["worst_standard"]["quiet"] is True
    assert result["worst_precheck"]["quiet"] is True


def test_improvement_calculation_and_ordering() -> None:
    latest = [
        row("AAA", 600),
        row("BBB", 1200),
        row("CCC", 900),
        row("DDD", 1800),
    ]
    baseline = [("AAA", 1800), ("BBB", 1200), ("CCC", 600), ("DDD", 2400)]
    result = leaderboard.build(
        latest,
        baseline,
        {iata: iata for iata in ("AAA", "BBB", "CCC", "DDD")},
        now=NOW,
    )

    entries = result["most_improved"]["entries"]
    assert [entry["iata"] for entry in entries] == ["AAA", "DDD"]
    assert entries[0]["previous_wait_seconds"] == 1800
    assert entries[0]["drop_seconds"] == 1200
    assert entries[1]["drop_seconds"] == 600
    assert result["most_improved"]["quiet"] is True


class FakeCursor:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool
        self.query = ""
        self.reference = datetime.now(UTC)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        self.query = query
        self.pool.executed.append((query, params))

    async def fetchall(self) -> list[Any]:
        if self.query == "SELECT iata, name FROM airports":
            return [("AAA", "Airport AAA"), ("BBB", "Airport BBB")]
        if self.query == main.LATEST_OBS_SQL:
            return [
                row("AAA", 600, reference=self.reference),
                row("BBB", 1200, lane="precheck", reference=self.reference),
            ]
        if self.query == main.LEADERBOARD_BASELINE_SQL:
            return [("AAA", 1800)]
        raise AssertionError(f"unexpected query: {self.query}")


class FakeConnection:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.pool)


class FakePool:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []

    def connection(self) -> FakeConnection:
        return FakeConnection(self)


def test_api_leaderboard_uses_parameterized_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool()
    monkeypatch.setattr(main.db, "pool", pool)

    response = asyncio.run(main.api_leaderboard())
    body = json.loads(response.body)

    assert set(body) >= {"worst_standard", "worst_precheck", "most_improved"}
    assert body["worst_standard"]["entries"][0]["iata"] == "AAA"
    assert body["worst_precheck"]["entries"][0]["iata"] == "BBB"
    baseline_calls = [params for query, params in pool.executed if query == main.LEADERBOARD_BASELINE_SQL]
    assert len(baseline_calls) == 1
    assert baseline_calls[0] is not None
    assert len(baseline_calls[0]) == 2
    assert all(isinstance(value, datetime) and value.tzinfo is not None for value in baseline_calls[0])
