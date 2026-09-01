"""Adapter behaviour on synthetic payloads: unit conversion and close/hide flags.

These complement the recorded fixtures, which only capture whatever state the
feeds happened to publish when they were probed.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.sources import adapters
from app.sources.base import FetchResult


def _run(fetch: Any, payload: Any) -> FetchResult:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def go() -> FetchResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch(client)

    return asyncio.run(go())


def test_sea_converts_minutes_to_seconds_and_keeps_closed_flag() -> None:
    result = _run(
        adapters.fetch_sea,
        [
            {"Name": "1", "WaitTimeMinutes": 12, "IsOpen": True},
            {"Name": "2", "WaitTimeMinutes": None, "IsOpen": False},
        ],
    )
    assert [(o.checkpoint_name, o.wait_seconds, o.is_open) for o in result.observations] == [
        ("Checkpoint 1", 720, True),
        ("Checkpoint 2", None, False),
    ]


def test_sea_zero_minute_wait_is_zero_seconds_not_none() -> None:
    result = _run(adapters.fetch_sea, [{"Name": "1", "WaitTimeMinutes": 0, "IsOpen": True}])
    assert result.observations[0].wait_seconds == 0


def test_den_skips_hidden_lanes_and_marks_closed_lanes() -> None:
    result = _run(
        adapters.fetch_den,
        [
            {
                "title": "East Security",
                "lanes": [
                    {"title": "Standard", "wait_time": "3-7", "closed": False},
                    {"title": "PreCheck", "wait_time": "2", "closed": True},
                    {"title": "Hidden", "wait_time": "9", "hide_lane": True},
                    {"title": "Forced", "wait_time": "9", "force_close": True},
                ],
            }
        ],
    )
    parsed = [(o.checkpoint_name, o.lane_type, o.wait_seconds, o.is_open) for o in result.observations]
    assert parsed == [
        ("East Security — Standard", "standard", 420, True),
        ("East Security — PreCheck", "precheck", 120, False),
        ("East Security — Forced", "standard", 540, False),
    ]


def test_den_missing_wait_time_is_none_not_zero() -> None:
    result = _run(
        adapters.fetch_den,
        [{"title": "West Security", "lanes": [{"title": "Standard", "wait_time": None}]}],
    )
    assert result.observations[0].wait_seconds is None
    assert result.observations[0].is_open is True


def _airportlabs_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": {"wait_times": rows}}


def test_airportlabs_respects_isdisplayable_and_isopen() -> None:
    result = _run(
        adapters.fetch_mco,
        _airportlabs_payload(
            [
                {"name": "South Standard", "lane": "standard", "waitSeconds": 229, "isOpen": True,
                 "lastUpdatedTimestamp": 1735689600},
                {"name": "South PreCheck", "lane": "precheck", "waitSeconds": 184, "isOpen": False,
                 "lastUpdatedTimestamp": 1735689600000},
                {"name": "Hidden", "lane": "standard", "waitSeconds": 60, "isDisplayable": False},
            ]
        ),
    )
    parsed = [(o.checkpoint_name, o.lane_type, o.wait_seconds, o.is_open) for o in result.observations]
    assert parsed == [
        ("South Standard", "standard", 229, True),
        ("South PreCheck", "precheck", 184, False),
    ]
    # Epoch seconds and epoch milliseconds must resolve to the same instant.
    assert result.observations[0].published_at == result.observations[1].published_at


def test_airportlabs_wait_seconds_are_passed_through_unscaled() -> None:
    result = _run(
        adapters.fetch_dfw,
        _airportlabs_payload([{"name": "C11", "lane": "standard", "waitSeconds": 519, "isOpen": True}]),
    )
    assert result.observations[0].wait_seconds == 519


def test_slc_converts_minutes_to_seconds() -> None:
    assert _run(adapters.fetch_slc, {"rightnow": "16"}).observations[0].wait_seconds == 960


def test_slc_missing_reading_is_none() -> None:
    assert _run(adapters.fetch_slc, {}).observations[0].wait_seconds is None


def test_las_converts_minutes_and_respects_open_flag() -> None:
    def payload(name: str) -> list[dict[str, Any]]:
        return [
            {
                "result": {
                    "data": {
                        "paths": {
                            "precheck": {
                                "name": "TSA PreCheck®",
                                "open": True,
                                "waitTime": {"value": 1.5, "timestamp": 1735689600},
                            },
                            "standard": {
                                "name": f"Standard {name}",
                                "open": False,
                                "waitTime": {"value": None},
                            },
                        }
                    }
                }
            }
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        journey = json.loads(request.url.params["input"])["0"]["journey"]
        return httpx.Response(200, json=payload(journey))

    async def go() -> FetchResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapters.fetch_las(client)

    result = asyncio.run(go())
    assert len(result.observations) == 2 * len(adapters.LAS_JOURNEYS)
    precheck = result.observations[0]
    standard = result.observations[1]
    assert (precheck.lane_type, precheck.wait_seconds, precheck.is_open) == ("precheck", 90, True)
    assert (standard.lane_type, standard.wait_seconds, standard.is_open) == ("standard", None, False)
    assert set(result.raw) == set(adapters.LAS_JOURNEYS)


@pytest.mark.parametrize("fetch", [adapters.fetch_sea, adapters.fetch_slc])
def test_adapters_raise_on_http_error(fetch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await fetch(client)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(go())
