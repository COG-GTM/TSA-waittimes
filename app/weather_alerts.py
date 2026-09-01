"""Active NWS weather alerts per airport, from the free public api.weather.gov.

One national ``/alerts/active`` request per cycle is matched against a cached
NWS zone per airport (resolved once via ``/points/{lat},{lon}``), so the number
of requests does not grow with the number of airports.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from . import db
from .sources.base import Source

log = logging.getLogger("weather")

SOURCE_CODE = "NWS-ALERTS"
SOURCE_NAME = "NWS active weather alerts"
# Public documentation page for the open api.weather.gov service (verified 2026-09-01).
PUBLIC_PAGE = "https://www.weather.gov/documentation/services-web-api"
ATTRIBUTION = "National Weather Service (weather.gov)"
ALERTS_URL = "https://api.weather.gov/alerts/active"
POINTS_URL = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
REFRESH_SECONDS = 600
ZONE_TTL = timedelta(days=90)
# NWS asks clients to be gentle; the zone backfill is a one-off walk of every airport.
POINTS_DELAY_SECONDS = 0.4
POINTS_PER_CYCLE = 150

# Aviation-relevant NWS event types only. Everything else the national feed
# carries (marine, hydrologic, heat/air-quality, fire-weather and the
# non-weather civil-emergency messages) is dropped: those dominate the feed by
# volume and do not describe conditions that disrupt a passenger's trip through
# an airport.
RELEVANT_EVENTS: frozenset[str] = frozenset({
    # Convective / severe storms
    "Extreme Wind Warning",
    "Severe Thunderstorm Warning",
    "Severe Thunderstorm Watch",
    "Severe Weather Statement",
    "Tornado Warning",
    "Tornado Watch",
    # Winter weather and icing
    "Blizzard Warning",
    "Extreme Cold Warning",
    "Extreme Cold Watch",
    "Freezing Fog Advisory",
    "Ice Storm Warning",
    "Lake Effect Snow Warning",
    "Snow Squall Warning",
    "Winter Storm Warning",
    "Winter Storm Watch",
    "Winter Weather Advisory",
    # Wind
    "High Wind Warning",
    "High Wind Watch",
    "Wind Advisory",
    # Visibility / airborne particulates
    "Ashfall Advisory",
    "Ashfall Warning",
    "Blowing Dust Advisory",
    "Blowing Dust Warning",
    "Dense Fog Advisory",
    "Dense Smoke Advisory",
    "Dust Advisory",
    "Dust Storm Warning",
    "Volcano Warning",
    # Tropical
    "Hurricane Warning",
    "Hurricane Watch",
    "Storm Surge Warning",
    "Storm Surge Watch",
    "Tropical Cyclone Local Statement",
    "Tropical Storm Warning",
    "Tropical Storm Watch",
    "Typhoon Warning",
    "Typhoon Watch",
    # Flooding severe enough to close airport roads/ramps (advisories excluded)
    "Flash Flood Warning",
})

SEVERITY_RANK: dict[str, int] = {
    "Extreme": 4,
    "Severe": 3,
    "Moderate": 2,
    "Minor": 1,
    "Unknown": 0,
}
MAX_TEXT = 500

# (lon, lat) vertices of one GeoJSON ring, and a polygon as (outer ring, holes).
Ring = tuple[tuple[float, float], ...]
Polygon = tuple[Ring, tuple[Ring, ...]]


@dataclass(frozen=True)
class AirportZones:
    airport_iata: str
    forecast_zone: str | None
    county_zone: str | None
    fire_zone: str | None

    @property
    def codes(self) -> set[str]:
        return {z for z in (self.forecast_zone, self.county_zone, self.fire_zone) if z}


@dataclass(frozen=True)
class Alert:
    alert_id: str
    event: str
    severity: str
    urgency: str | None
    certainty: str | None
    headline: str | None
    area_desc: str | None
    sender_name: str | None
    alert_url: str | None
    effective: datetime | None
    onset: datetime | None
    expires: datetime | None
    ends: datetime | None
    zones: frozenset[str]
    polygons: tuple[Polygon, ...]

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 0)


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    """Coerce an untrusted feed value to a bounded string (None when absent)."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value[:limit] or None


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _zone_code(url: Any) -> str | None:
    """``https://api.weather.gov/zones/forecast/WAZ316`` -> ``WAZ316``."""
    text = _text(url, limit=300)
    if text is None:
        return None
    code = text.rstrip("/").rsplit("/", 1)[-1].upper()
    return code if code.isalnum() and 3 <= len(code) <= 12 else None


def parse_zones(iata: str, payload: Any) -> AirportZones | None:
    """Parse an api.weather.gov ``/points`` response into the zones we cache."""
    if not isinstance(payload, dict):
        return None
    props = payload.get("properties")
    if not isinstance(props, dict):
        return None
    zones = AirportZones(
        airport_iata=iata,
        forecast_zone=_zone_code(props.get("forecastZone")),
        county_zone=_zone_code(props.get("county")),
        fire_zone=_zone_code(props.get("fireWeatherZone")),
    )
    return zones if zones.codes else None


def _ring(raw: Any) -> Ring | None:
    if not isinstance(raw, list):
        return None
    points = tuple(
        (float(p[0]), float(p[1]))
        for p in raw
        if isinstance(p, list | tuple) and len(p) >= 2
        and isinstance(p[0], int | float) and isinstance(p[1], int | float)
    )
    return points if len(points) >= 3 else None


def _polygons(geometry: Any) -> tuple[Polygon, ...]:
    """GeoJSON Polygon/MultiPolygon as (outer ring, interior rings) in (lon, lat)."""
    if not isinstance(geometry, dict):
        return ()
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        raw_polys: list[Any] = [coords]
    elif gtype == "MultiPolygon" and isinstance(coords, list):
        raw_polys = list(coords)
    else:
        return ()
    polygons: list[Polygon] = []
    for raw in raw_polys:
        if not isinstance(raw, list) or not raw:
            continue
        outer = _ring(raw[0])
        if outer is None:
            continue
        holes = tuple(h for h in (_ring(r) for r in raw[1:]) if h is not None)
        polygons.append((outer, holes))
    return tuple(polygons)


def point_in_polygon(lon: float, lat: float, polygon: Polygon) -> bool:
    """Inside the outer ring and outside every hole the alert excludes."""
    outer, holes = polygon
    if not point_in_ring(lon, lat, outer):
        return False
    return not any(point_in_ring(lon, lat, hole) for hole in holes)


def point_in_ring(lon: float, lat: float, ring: Ring) -> bool:
    """Ray-casting test; rings are small NWS warning polygons, so planar math is fine."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_at:
                inside = not inside
    return inside


def parse_alert(feature: Any) -> Alert | None:
    """Parse one GeoJSON feature from ``/alerts/active``; None when not relevant."""
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties")
    if not isinstance(props, dict):
        return None
    event = _text(props.get("event"), limit=100)
    alert_id = _text(props.get("id"), limit=300)
    if not event or not alert_id or event not in RELEVANT_EVENTS:
        return None
    if _text(props.get("status"), limit=32) != "Actual":
        return None
    geocode = props.get("geocode")
    ugc = geocode.get("UGC") if isinstance(geocode, dict) else None
    zones = {
        code
        for code in (_zone_code(z) for z in (ugc or []) if isinstance(z, str))
        if code
    }
    for zone_url in props.get("affectedZones") or []:
        code = _zone_code(zone_url)
        if code:
            zones.add(code)
    return Alert(
        alert_id=alert_id,
        event=event,
        severity=_text(props.get("severity"), limit=32) or "Unknown",
        urgency=_text(props.get("urgency"), limit=32),
        certainty=_text(props.get("certainty"), limit=32),
        headline=_text(props.get("headline")),
        area_desc=_text(props.get("areaDesc")),
        sender_name=_text(props.get("senderName"), limit=120),
        alert_url=_text(props.get("@id"), limit=300),
        effective=_dt(props.get("effective")),
        onset=_dt(props.get("onset")),
        expires=_dt(props.get("expires")),
        ends=_dt(props.get("ends")),
        zones=frozenset(zones),
        polygons=_polygons(feature.get("geometry")),
    )


def parse_alerts(payload: Any) -> list[Alert]:
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        return []
    parsed = (parse_alert(f) for f in features)
    return [a for a in parsed if a is not None]


def match_alerts(
    alerts: list[Alert],
    zones_by_airport: dict[str, AirportZones],
    coords: dict[str, tuple[float, float]],
    *,
    now: datetime | None = None,
) -> dict[str, list[tuple[Alert, str]]]:
    """Map airport IATA -> [(alert, match_basis)] for currently effective alerts."""
    now = now or datetime.now(UTC)
    by_zone: dict[str, list[str]] = {}
    for iata, zones in zones_by_airport.items():
        for code in zones.codes:
            by_zone.setdefault(code, []).append(iata)
    matched: dict[str, list[tuple[Alert, str]]] = {}
    for alert in alerts:
        if alert.expires is not None and alert.expires <= now:
            continue
        hit: dict[str, str] = {}
        if alert.polygons:
            # Storm-based warnings carry both a polygon and the whole counties it
            # clips; the polygon is the warned area, so geometry wins outright and
            # is tested against every airport we have coordinates for.
            for polygon in alert.polygons:
                for iata, (lat, lon) in coords.items():
                    if point_in_polygon(lon, lat, polygon):
                        hit[iata] = "polygon"
        else:
            for code in alert.zones:
                for iata in by_zone.get(code, ()):
                    hit[iata] = "zone"
        for iata, basis in hit.items():
            matched.setdefault(iata, []).append((alert, basis))
    for iata, entries in matched.items():
        matched[iata] = _dedupe(entries)
    return matched


def _dedupe(entries: list[tuple[Alert, str]]) -> list[tuple[Alert, str]]:
    """Collapse reissues of the same product, keeping the newest.

    A reissue repeats the event, the issuing office and the covered zones, so
    that triple is the key: two distinct warnings of the same type (different
    office or different area) stay as separate rows.

    Ordered most severe first so the API's headline alert is entries[0].
    """
    newest: dict[tuple[str, str | None, frozenset[str]], tuple[Alert, str]] = {}
    for alert, basis in entries:
        key = (alert.event, alert.sender_name, alert.zones)
        current = newest.get(key)
        if current is None or _issued(alert) > _issued(current[0]):
            newest[key] = (alert, basis)
    return sorted(newest.values(), key=lambda e: (-e[0].rank, e[0].event))


def _issued(alert: Alert) -> datetime:
    return alert.effective or alert.onset or datetime.min.replace(tzinfo=UTC)


async def fetch_alerts(client: httpx.AsyncClient) -> Any:
    resp = await client.get(
        ALERTS_URL,
        params={"status": "actual", "message_type": "alert,update"},
        headers={"Accept": "application/geo+json"},
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_zones(client: httpx.AsyncClient, iata: str, lat: float, lon: float) -> AirportZones | None:
    resp = await client.get(
        POINTS_URL.format(lat=lat, lon=lon), headers={"Accept": "application/geo+json"}
    )
    resp.raise_for_status()
    return parse_zones(iata, resp.json())


async def load_zone_cache() -> dict[str, AirportZones]:
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT airport_iata, forecast_zone, county_zone, fire_zone FROM airport_nws_zones"
        )
        return {
            row[0]: AirportZones(airport_iata=row[0], forecast_zone=row[1], county_zone=row[2], fire_zone=row[3])
            for row in await cur.fetchall()
        }


async def zone_coverage() -> tuple[int, int]:
    """(airports with a cached NWS zone, airports in total)."""
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT (SELECT count(*) FROM airport_nws_zones), (SELECT count(*) FROM airports)
            """
        )
        row = await cur.fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)


async def load_coords() -> dict[str, tuple[float, float]]:
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT iata, lat, lon FROM airports")
        return {row[0]: (row[1], row[2]) for row in await cur.fetchall()}


async def _airports_missing_zones(limit: int) -> list[tuple[str, float, float]]:
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT a.iata, a.lat, a.lon
            FROM airports a
            LEFT JOIN airport_nws_zones z ON z.airport_iata = a.iata
            WHERE z.airport_iata IS NULL OR z.fetched_at < now() - %s::interval
            ORDER BY a.iata
            LIMIT %s
            """,
            (ZONE_TTL, limit),
        )
        return [(r[0], r[1], r[2]) for r in await cur.fetchall()]


async def _store_zones(zones: AirportZones) -> None:
    assert db.pool is not None
    async with db.pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO airport_nws_zones (airport_iata, forecast_zone, county_zone, fire_zone, fetched_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (airport_iata) DO UPDATE SET
                forecast_zone = EXCLUDED.forecast_zone,
                county_zone = EXCLUDED.county_zone,
                fire_zone = EXCLUDED.fire_zone,
                fetched_at = now()
            """,
            (zones.airport_iata, zones.forecast_zone, zones.county_zone, zones.fire_zone),
        )


async def refresh_zone_cache(client: httpx.AsyncClient, *, limit: int = POINTS_PER_CYCLE) -> int:
    """Resolve NWS zones for airports that have none cached yet (a few per cycle)."""
    resolved = 0
    for iata, lat, lon in await _airports_missing_zones(limit):
        try:
            zones = await fetch_zones(client, iata, lat, lon)
        except Exception as err:  # noqa: BLE001 - one bad point must not stop the backfill
            log.warning("nws zone lookup failed for %s: %s", iata, err)
            continue
        if zones is None:
            log.warning("nws zone lookup for %s returned no usable zone", iata)
            continue
        await _store_zones(zones)
        resolved += 1
        await asyncio.sleep(POINTS_DELAY_SECONDS)
    return resolved


async def store_alerts(matched: dict[str, list[tuple[Alert, str]]], raw_id: int | None) -> int:
    """Replace the current alert set; airports with no active alert end up with no rows."""
    assert db.pool is not None
    stored = 0
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM weather_alerts")
        for iata, entries in matched.items():
            for alert, basis in entries:
                await cur.execute(
                    """
                    INSERT INTO weather_alerts (
                        airport_iata, alert_id, event, severity, urgency, certainty,
                        headline, area_desc, sender_name, alert_url,
                        effective, onset, expires, ends, match_basis, fetched_at, raw_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
                    ON CONFLICT (airport_iata, alert_id) DO NOTHING
                    """,
                    (
                        iata, alert.alert_id, alert.event, alert.severity, alert.urgency,
                        alert.certainty, alert.headline, alert.area_desc, alert.sender_name,
                        alert.alert_url, alert.effective, alert.onset, alert.expires,
                        alert.ends, basis, raw_id,
                    ),
                )
                stored += 1
    return stored


SOURCE = Source(
    code=SOURCE_CODE,
    name=SOURCE_NAME,
    url=PUBLIC_PAGE,
    attribution=ATTRIBUTION,
    refresh_seconds=REFRESH_SECONDS,
    fetch=fetch_alerts,
)
