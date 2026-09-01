import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.faa_events import FAA_EVENTS_URL, _parse_duration_seconds, fetch_faa_events, parse_airport_events

FIXTURE = Path(__file__).parent / "fixtures" / "faa_airport_events.json"


def test_fixture_parses_expected_events() -> None:
    payload = json.loads(FIXTURE.read_text())
    events = parse_airport_events(payload)
    assert {(event.airport_iata, event.event_type) for event in events} == {
        ("BOS", "ground_delay"),
        ("EWR", "ground_delay"),
        ("HPN", "departure_delay"),
        ("JFK", "ground_delay"),
        ("LGA", "ground_delay"),
        ("LGA", "departure_delay"),
        ("LMT", "closure"),
        ("SAN", "ground_delay"),
        ("SFO", "ground_delay"),
        ("SNA", "closure"),
    }
    bos = next(event for event in events if event.airport_iata == "BOS")
    assert bos.avg_delay_seconds == 8820
    assert bos.update_time is not None and bos.update_time.tzinfo is not None

    departure = next(
        event
        for event in events
        if event.airport_iata == "LGA" and event.event_type == "departure_delay"
    )
    assert departure.avg_delay_seconds == 1800


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 hour and 42 minutes", 6120),
        ("45 minutes", 2700),
        ("2 hours", 7200),
        ("garbage", None),
    ],
)
def test_parse_duration_seconds(text: str, expected: int | None) -> None:
    assert _parse_duration_seconds(text) == expected


def test_fetch_faa_events_with_mock_transport() -> None:
    payload = json.loads(FIXTURE.read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == FAA_EVENTS_URL
        return httpx.Response(200, json=payload)

    async def run() -> tuple[object, int]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            raw, events = await fetch_faa_events(client)
            return raw, len(events)

    raw, count = asyncio.run(run())
    assert raw == payload
    assert count == 10


def test_parse_rejects_non_list_and_skips_malformed_entries() -> None:
    with pytest.raises(ValueError):
        parse_airport_events({})
    payload = [
        {},
        {"airportId": "abc", "groundDelay": {"avgDelay": "bad"}},
        {"airportId": "def", "groundDelay": "bad"},
        {"airportId": "ghi", "airportClosure": {"simpleText": 3}},
    ]
    assert parse_airport_events(payload) == []
