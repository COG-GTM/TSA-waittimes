"""Replay one recorded payload per adapter and assert the parsed observations.

Fixtures are recorded by ``python scripts/probe_source.py <CODE>``. Dropping a
new fixture file into ``tests/fixtures/`` is all a new adapter needs; these
tests discover it automatically. No test here touches the network.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.sources.adapters import SOURCES
from tests.harness import (
    FIXTURE_DIR,
    fixture_path,
    load_fixture,
    observation_to_dict,
    replay,
    replay_transport,
)

LANE_TYPES = {"standard", "precheck", "other"}
MAX_PLAUSIBLE_WAIT_SECONDS = 6 * 3600
SOURCES_BY_CODE = {s.code: s for s in SOURCES}
FIXTURE_FILES = sorted(FIXTURE_DIR.glob("*.json"))


def _codes() -> list[str]:
    return [p.stem.upper() for p in FIXTURE_FILES]


@pytest.fixture(scope="module", params=_codes())
def code(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(scope="module")
def fixture(code: str) -> dict[str, Any]:
    return load_fixture(fixture_path(code))


@pytest.fixture(scope="module")
def parsed(code: str, fixture: dict[str, Any]) -> list[dict[str, Any]]:
    result = asyncio.run(replay(SOURCES_BY_CODE[code], fixture))
    return [observation_to_dict(ob) for ob in result.observations]


def test_every_adapter_has_a_fixture() -> None:
    missing = [s.code for s in SOURCES if not fixture_path(s.code).exists()]
    assert not missing, (
        f"no recorded fixture for {missing}; run: python scripts/probe_source.py {missing[0] if missing else ''}"
    )


def test_fixture_belongs_to_a_known_source(code: str, fixture: dict[str, Any]) -> None:
    assert fixture["source"] == code
    assert code in SOURCES_BY_CODE
    assert fixture["exchanges"], "fixture recorded no HTTP exchanges"


def test_parsed_matches_recorded_observations(parsed: list[dict[str, Any]], fixture: dict[str, Any]) -> None:
    assert parsed == fixture["expected"]["observations"]


def test_checkpoint_count(parsed: list[dict[str, Any]], fixture: dict[str, Any]) -> None:
    expected = fixture["expected"]
    assert len(parsed) == expected["observation_count"]
    assert parsed, "an empty successful fetch is not a healthy poll"


def test_lane_type_classification(parsed: list[dict[str, Any]], fixture: dict[str, Any]) -> None:
    counts: dict[str, int] = {}
    for ob in parsed:
        assert ob["lane_type"] in LANE_TYPES
        counts[ob["lane_type"]] = counts.get(ob["lane_type"], 0) + 1
    assert counts == fixture["expected"]["lane_types"]
    for ob in parsed:
        if "precheck" in ob["checkpoint_name"].lower().replace(" ", ""):
            assert ob["lane_type"] == "precheck", ob["checkpoint_name"]


def test_wait_seconds_are_seconds(parsed: list[dict[str, Any]], fixture: dict[str, Any]) -> None:
    waits = [ob["wait_seconds"] for ob in parsed if ob["wait_seconds"] is not None]
    for wait in waits:
        assert isinstance(wait, int)
        assert 0 <= wait <= MAX_PLAUSIBLE_WAIT_SECONDS
    assert len(parsed) - len(waits) == fixture["expected"]["null_wait_count"]
    assert (max(waits) if waits else None) == fixture["expected"]["max_wait_seconds"]


def test_open_closed_flags(parsed: list[dict[str, Any]], fixture: dict[str, Any]) -> None:
    expected = fixture["expected"]
    for ob in parsed:
        assert isinstance(ob["is_open"], bool)
    assert sum(1 for ob in parsed if ob["is_open"]) == expected["open_count"]
    assert sum(1 for ob in parsed if not ob["is_open"]) == expected["closed_count"]


def test_timestamps_are_timezone_aware(parsed: list[dict[str, Any]], fixture: dict[str, Any]) -> None:
    published = [ob["published_at"] for ob in parsed if ob["published_at"]]
    assert len(published) == fixture["expected"]["with_published_at"]
    for value in published:
        parsed_dt = datetime.fromisoformat(value)
        assert parsed_dt.tzinfo is not None
        # Epoch-milliseconds read as seconds would land in the far future.
        assert parsed_dt.year <= datetime.now(tz=UTC).year + 1


def test_replay_rejects_unrecorded_requests(fixture: dict[str, Any]) -> None:
    async def call_unrecorded() -> None:
        async with httpx.AsyncClient(transport=replay_transport(fixture["exchanges"])) as client:
            await client.get("https://example.invalid/not-recorded")

    with pytest.raises(AssertionError):
        asyncio.run(call_unrecorded())


def test_fixture_dir_contains_only_json() -> None:
    stray = [p.name for p in FIXTURE_DIR.iterdir() if p.suffix != ".json"]
    assert not stray, f"unexpected files in {Path(FIXTURE_DIR).name}: {stray}"
