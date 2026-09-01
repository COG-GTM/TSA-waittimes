import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.faa_events import FAA_EVENTS_URL, _parse_duration_seconds, fetch_faa_events, parse_airport_events

FIXTURE = Path(__file__).parent / "fixtures" / "faa_airport_events.json"
FIXTURE_NOW = datetime(2026, 9, 1, 8, tzinfo=UTC)


def test_fixture_parses_expected_events() -> None:
    payload = json.loads(FIXTURE.read_text())
    events = parse_airport_events(payload, now=FIXTURE_NOW)
    assert {(event.airport_iata, event.event_type) for event in events} == {
        ("BOS", "ground_delay"),
        ("EWR", "ground_delay"),
        ("HPN", "departure_delay"),
        ("JFK", "ground_delay"),
        ("LGA", "ground_delay"),
        ("LGA", "departure_delay"),
        ("LFT", "closure"),
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
            raw, events = await fetch_faa_events(client, now=FIXTURE_NOW)
            return raw, len(events)

    raw, count = asyncio.run(run())
    assert raw == payload
    assert count == 11


def test_daily_closure_is_active_only_during_window() -> None:
    payload = [{
        "airportId": "ABC",
        "airportClosure": {
            "simpleText": "ABC AD AP CLSD DLY 2300-0400",
            "text": "DLY 2300-0400",
            "startTime": "2026-09-01T00:00:00Z",
            "endTime": "2026-09-02T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        },
    }]
    assert len(parse_airport_events(payload, now=datetime(2026, 9, 1, 23, tzinfo=UTC))) == 1
    assert len(parse_airport_events(payload, now=datetime(2026, 9, 1, 12, tzinfo=UTC))) == 0


def test_unparseable_daily_closure_is_skipped() -> None:
    payload = [{
        "airportId": "ABC",
        "airportClosure": {
            "simpleText": "ABC AD AP CLSD DLY unknown",
            "text": "DLY unknown",
            "startTime": "2026-09-01T00:00:00Z",
            "endTime": "2026-09-02T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        },
    }]
    assert parse_airport_events(payload, now=FIXTURE_NOW) == []


def test_traffic_restriction_free_form_closures_are_skipped() -> None:
    payload = [
        {
            "airportId": "LAX",
            "freeForm": {
                "simpleText": (
                    "!LAX 05/277 LAX AD AP CLSD TO NON SKED TRANSIENT GA ACFT "
                    "EXC 24HR PPR CTC ATLANTIC AVIATION 310-258-9884 OR "
                    "SIGNATURE AVIATION 310-410-9605 2605271826-2705281600"
                ),
                "text": (
                    "!LAX 05/277 LAX AD AP CLSD TO NON SKED TRANSIENT GA ACFT "
                    "EXC 24HR PPR CTC ATLANTIC AVIATION 310-258-9884 OR "
                    "SIGNATURE AVIATION 310-410-9605 2605271826-2705281600"
                ),
                "startTime": "2026-05-27T18:26:00Z",
                "endTime": "2027-05-28T16:00:00Z",
                "updatedAt": "2026-05-27T18:26:00Z",
            },
        },
        {
            "airportId": "SAN",
            "freeForm": {
                "simpleText": (
                    "!SAN 03/071 SAN AD AP CLSD TO NON SKED TRANSIENT GA ACFT "
                    "EXC PPR 619-298-7704 2603181300-2610010800"
                ),
                "text": (
                    "!SAN 03/071 SAN AD AP CLSD TO NON SKED TRANSIENT GA ACFT "
                    "EXC PPR 619-298-7704 2603181300-2610010800"
                ),
                "startTime": "2026-03-18T13:00:00Z",
                "endTime": "2026-10-01T08:00:00Z",
                "updatedAt": "2026-03-18T13:00:00Z",
            },
        },
    ]
    assert parse_airport_events(payload, now=FIXTURE_NOW) == []


def test_free_form_closure_with_exception_still_parses() -> None:
    payload = [{
        "airportId": "ABC",
        "freeForm": {
            "simpleText": "ABC AD AP CLSD EXC MEDEVAC",
            "text": "ABC AD AP CLSD EXC MEDEVAC",
            "startTime": "2026-09-01T00:00:00Z",
            "endTime": "2026-09-02T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        },
    }]
    events = parse_airport_events(payload, now=FIXTURE_NOW)
    assert [(event.airport_iata, event.event_type) for event in events] == [("ABC", "closure")]


def test_lft_fixture_free_form_closure_still_parses() -> None:
    payload = json.loads(FIXTURE.read_text())
    entry = next(item for item in payload if item["airportId"] == "LFT")
    events = parse_airport_events([entry], now=FIXTURE_NOW)
    assert [(event.airport_iata, event.event_type) for event in events] == [("LFT", "closure")]


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
