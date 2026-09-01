"""Replay one recorded api.weather.gov cycle and assert the matching we ship.

The fixture is recorded by ``python scripts/probe_weather_alerts.py``: real
``/points`` payloads for every airport plus the alert features that matched
them, so nothing here touches the network.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import weather_alerts as wx
from app.sources.base import USER_AGENT

FIXTURE = Path(__file__).parent / "weather_fixtures" / "nws_alerts.json"
AIRPORTS = Path(__file__).parent.parent / "data" / "us_airports.json"
EVENT_TYPES = Path(__file__).parent / "weather_fixtures" / "nws_event_types.json"

# Categories the relevance filter deliberately drops; no whitelisted event may
# look like one of these.
EXCLUDED_KEYWORDS = (
    "small craft", "gale", "hazardous seas", "marine", "rip current", "surf",
    "beach", "freezing spray", "low water", "heat", "air quality",
    "air stagnation", "red flag", "fire weather", "amber", "blue alert",
    "telephone outage", "child abduction", "administrative", "test",
)


@pytest.fixture(scope="module")
def fixture() -> dict[str, Any]:
    with FIXTURE.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def captured_at(fixture: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(fixture["captured_at"])


@pytest.fixture(scope="module")
def coords() -> dict[str, tuple[float, float]]:
    with AIRPORTS.open(encoding="utf-8") as fh:
        return {a["iata"]: (a["lat"], a["lon"]) for a in json.load(fh)}


@pytest.fixture(scope="module")
def zones(fixture: dict[str, Any]) -> dict[str, wx.AirportZones]:
    parsed = {iata: wx.parse_zones(iata, payload) for iata, payload in fixture["points"].items()}
    return {iata: z for iata, z in parsed.items() if z is not None}


@pytest.fixture(scope="module")
def alerts(fixture: dict[str, Any]) -> list[wx.Alert]:
    return wx.parse_alerts(fixture["alerts"])


@pytest.fixture(scope="module")
def matched(
    alerts: list[wx.Alert],
    zones: dict[str, wx.AirportZones],
    coords: dict[str, tuple[float, float]],
    captured_at: datetime,
) -> dict[str, list[tuple[wx.Alert, str]]]:
    return wx.match_alerts(alerts, zones, coords, now=captured_at)


def test_whitelist_only_names_real_nws_event_types() -> None:
    with EVENT_TYPES.open(encoding="utf-8") as fh:
        official = set(json.load(fh)["eventTypes"])
    unknown = wx.RELEVANT_EVENTS - official
    assert not unknown, f"not real api.weather.gov event types: {sorted(unknown)}"


def test_whitelist_excludes_the_documented_noise_categories() -> None:
    leaked = [
        event for event in wx.RELEVANT_EVENTS
        if any(word in event.lower() for word in EXCLUDED_KEYWORDS)
    ]
    assert not leaked, f"excluded category leaked into the whitelist: {sorted(leaked)}"


def test_zone_parsing_matches_recorded_points(fixture: dict[str, Any], zones: dict[str, wx.AirportZones]) -> None:
    assert zones, "fixture recorded no /points payloads"
    assert set(zones) == set(fixture["points"])
    for iata, parsed in zones.items():
        props = fixture["points"][iata]["properties"]
        assert parsed.forecast_zone == props["forecastZone"].rsplit("/", 1)[-1]
        assert parsed.county_zone == props["county"].rsplit("/", 1)[-1]
        assert parsed.codes


def test_irrelevant_events_are_dropped(fixture: dict[str, Any], alerts: list[wx.Alert]) -> None:
    dropped = fixture["expected"]["dropped_events"]
    assert dropped, "fixture recorded no irrelevant alerts to prove the filter works"
    kept = {a.event for a in alerts}
    assert not kept & set(dropped)
    assert kept <= wx.RELEVANT_EVENTS


def test_matching_reproduces_the_recorded_result(
    fixture: dict[str, Any], matched: dict[str, list[tuple[wx.Alert, str]]]
) -> None:
    actual = {
        iata: [
            {"event": a.event, "severity": a.severity, "basis": basis, "alert_id": a.alert_id}
            for a, basis in entries
        ]
        for iata, entries in sorted(matched.items())
    }
    assert actual == fixture["expected"]["matched"]


def test_matches_are_justified_by_zone_or_polygon(
    matched: dict[str, list[tuple[wx.Alert, str]]],
    zones: dict[str, wx.AirportZones],
    coords: dict[str, tuple[float, float]],
) -> None:
    for iata, entries in matched.items():
        lat, lon = coords[iata]
        for alert, basis in entries:
            if basis == "zone":
                assert alert.zones & zones[iata].codes
            else:
                assert any(wx.point_in_polygon(lon, lat, poly) for poly in alert.polygons)


def test_alerts_are_ordered_most_severe_first(matched: dict[str, list[tuple[wx.Alert, str]]]) -> None:
    for entries in matched.values():
        ranks = [a.rank for a, _ in entries]
        assert ranks == sorted(ranks, reverse=True)


def test_reissued_statements_collapse_to_the_newest(
    alerts: list[wx.Alert],
    zones: dict[str, wx.AirportZones],
    coords: dict[str, tuple[float, float]],
    captured_at: datetime,
) -> None:
    # The recorded feed carries several reissues of the same event for one office.
    reissued = [a for a in alerts if a.event == "Tropical Cyclone Local Statement"]
    assert len(reissued) > 1
    matched = wx.match_alerts(alerts, zones, coords, now=captured_at)
    for entries in matched.values():
        keys = [(a.event, a.sender_name, a.zones) for a, _ in entries]
        assert len(keys) == len(set(keys))
        for alert, _ in entries:
            same_event = [
                other for other in reissued
                if other.event == alert.event and other.zones & alert.zones
            ]
            if same_event and alert.effective is not None:
                assert alert.effective == max(
                    o.effective for o in same_event if o.effective is not None
                )


def test_timestamps_are_timezone_aware(alerts: list[wx.Alert], captured_at: datetime) -> None:
    assert alerts, "fixture recorded no relevant alerts"
    for alert in alerts:
        for value in (alert.effective, alert.onset, alert.expires, alert.ends):
            if value is not None:
                assert value.tzinfo is not None
                assert value.year <= captured_at.year + 1


def test_expired_alerts_are_not_matched(
    alerts: list[wx.Alert], zones: dict[str, wx.AirportZones], coords: dict[str, tuple[float, float]]
) -> None:
    expiries = [a.expires for a in alerts if a.expires is not None]
    assert expiries, "fixture recorded no alert expiry times"
    later = max(expiries) + timedelta(seconds=1)
    assert wx.match_alerts(alerts, zones, coords, now=later) == {}


def test_bad_payloads_do_not_raise() -> None:
    assert wx.parse_alerts(None) == []
    assert wx.parse_alerts({"features": "nope"}) == []
    assert wx.parse_alerts({"features": [None, 7, {}, {"properties": {}}]}) == []
    assert wx.parse_zones("XXX", {"properties": {}}) is None
    assert wx.parse_zones("XXX", "not json") is None


def test_untrusted_strings_are_bounded() -> None:
    feature = {
        "properties": {
            "id": "urn:oid:test",
            "event": "Winter Storm Warning",
            "status": "Actual",
            "severity": "Severe",
            "headline": "x" * 5000,
            "areaDesc": "y" * 5000,
            "geocode": {"UGC": ["WAZ316"]},
        }
    }
    alert = wx.parse_alert(feature)
    assert alert is not None
    assert alert.headline is not None and len(alert.headline) == wx.MAX_TEXT
    assert alert.area_desc is not None and len(alert.area_desc) == wx.MAX_TEXT
    assert alert.zones == frozenset({"WAZ316"})


def test_non_actual_status_is_ignored() -> None:
    feature = {
        "properties": {
            "id": "urn:oid:test-exercise",
            "event": "Tornado Warning",
            "status": "Exercise",
            "severity": "Extreme",
            "geocode": {"UGC": ["OKZ001"]},
        }
    }
    assert wx.parse_alert(feature) is None


def test_polygon_matching_is_inside_only() -> None:
    ring = ((-100.0, 40.0), (-99.0, 40.0), (-99.0, 41.0), (-100.0, 41.0))
    assert wx.point_in_ring(-99.5, 40.5, ring)
    assert not wx.point_in_ring(-98.5, 40.5, ring)
    assert not wx.point_in_ring(-99.5, 42.0, ring)


def _square(x1: float, y1: float, x2: float, y2: float) -> list[list[float]]:
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]


def test_polygon_holes_exclude_the_point() -> None:
    geometry = {
        "type": "Polygon",
        "coordinates": [_square(-100.0, 40.0, -98.0, 42.0), _square(-99.5, 40.5, -99.0, 41.0)],
    }
    (polygon,) = wx._polygons(geometry)
    assert polygon[1], "interior ring was discarded"
    assert wx.point_in_polygon(-98.5, 41.5, polygon)
    assert not wx.point_in_polygon(-99.25, 40.75, polygon)


def test_polygon_alert_skips_zone_neighbours_outside_the_warning() -> None:
    """A storm-based warning clips whole counties, so geometry beats the UGC zone."""
    feature = {
        "geometry": {"type": "Polygon", "coordinates": [_square(-100.0, 40.0, -99.0, 41.0)]},
        "properties": {
            "id": "urn:oid:poly", "@id": "https://api.weather.gov/alerts/urn:oid:poly",
            "event": "Severe Thunderstorm Warning", "status": "Actual", "severity": "Severe",
            "geocode": {"UGC": ["NEC001"]},
        },
    }
    alert = wx.parse_alert(feature)
    assert alert is not None
    zones = {
        "INS": wx.AirportZones("INS", "NEC001", None, None),
        "OUT": wx.AirportZones("OUT", "NEC001", None, None),
    }
    coords = {"INS": (40.5, -99.5), "OUT": (40.5, -98.5)}
    matched = wx.match_alerts([alert], zones, coords)
    assert set(matched) == {"INS"}
    assert matched["INS"][0][1] == "polygon"


def test_multipolygon_holes_are_per_component() -> None:
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [
            [_square(-100.0, 40.0, -98.0, 42.0), _square(-99.5, 40.5, -99.0, 41.0)],
            [_square(-90.0, 30.0, -88.0, 32.0)],
        ],
    }
    first, second = wx._polygons(geometry)
    assert not wx.point_in_polygon(-99.25, 40.75, first)
    assert wx.point_in_polygon(-89.0, 31.0, second)
    assert not second[1]


def test_fetch_alerts_sends_the_documented_query() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["ua"] = request.headers.get("User-Agent")
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    async def run() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), headers={"User-Agent": USER_AGENT}
        ) as client:
            return await wx.fetch_alerts(client)

    payload = asyncio.run(run())
    assert payload["features"] == []
    assert seen["url"].startswith(wx.ALERTS_URL)
    assert "status=actual" in seen["url"]
    assert "message_type=alert%2Cupdate" in seen["url"]
    assert seen["ua"] == USER_AGENT


def test_refresh_seconds_respects_the_polite_floor() -> None:
    assert wx.SOURCE.refresh_seconds >= 60
    assert wx.SOURCE.attribution == "National Weather Service (weather.gov)"


def test_severity_ranking_is_total_for_recorded_severities(alerts: list[wx.Alert]) -> None:
    assert set(wx.SEVERITY_RANK) == {"Extreme", "Severe", "Moderate", "Minor", "Unknown"}
    for alert in alerts:
        assert alert.severity in wx.SEVERITY_RANK


def test_utc_now_default_does_not_crash(
    alerts: list[wx.Alert], zones: dict[str, wx.AirportZones], coords: dict[str, tuple[float, float]]
) -> None:
    result = wx.match_alerts(alerts, zones, coords)
    assert isinstance(result, dict)
    assert datetime.now(UTC).tzinfo is UTC
