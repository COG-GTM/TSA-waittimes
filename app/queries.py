"""Shared database queries and data-shaping helpers."""
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import weather_alerts
from .faa_events import FAA_ATTRIBUTION, FAA_SOURCE_CODE
from .travel_calendar import TravelPeriod, period_payload

STALE_SECONDS = 30 * 60
EASTERN = ZoneInfo("America/New_York")
FAA_EVENT_SEVERITY = {
    "ground_stop": 5,
    "closure": 4,
    "ground_delay": 3,
    "arrival_delay": 2,
    "departure_delay": 1,
}


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


ALERT_COLUMNS = """
SELECT airport_iata, event, severity, headline, area_desc, sender_name, alert_url,
       effective, onset, expires, ends, fetched_at
FROM weather_alerts
"""
ACTIVE_ALERTS_SQL = ALERT_COLUMNS + "WHERE expires IS NULL OR expires > now()"
AIRPORT_ALERTS_SQL = ALERT_COLUMNS + "WHERE airport_iata = %s AND (expires IS NULL OR expires > now())"


def alert_dict(row: Sequence[Any]) -> dict[str, Any]:
    (_iata, event, severity, headline, area_desc, sender, url,
     effective, onset, expires, ends, fetched_at) = row
    return {
        "event": event,
        "severity": severity,
        "headline": headline,
        "area": area_desc,
        "sender": sender,
        "url": url,
        "effective": iso(effective),
        "onset": iso(onset),
        "expires": iso(expires),
        "ends": iso(ends),
        "fetched_at": iso(fetched_at),
        "source": weather_alerts.ATTRIBUTION,
        "source_url": weather_alerts.PUBLIC_PAGE,
    }


def alert_sort_key(alert: dict[str, Any]) -> tuple[int, str]:
    return (-weather_alerts.SEVERITY_RANK.get(alert["severity"], 0), alert["event"])


def today() -> date:
    return datetime.now(UTC).astimezone(EASTERN).date()


async def travel_period(cur, current_date: date | None = None) -> dict | None:
    current_date = current_date or today()
    await cur.execute(
        """
        SELECT name, start_date, end_date, intensity, note
        FROM travel_periods
        WHERE end_date >= %s
        ORDER BY start_date, name
        LIMIT 1
        """,
        (current_date,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    period = TravelPeriod(
        name=row[0],
        start=row[1],
        end=row[2],
        intensity=row[3],
        note=row[4],
    )
    return period_payload(period, current_date)


LATEST_OBS_SQL = """
SELECT c.airport_iata, c.name, c.lane_type, latest.wait_seconds, latest.is_open,
       latest.source_published_at, latest.fetched_at, latest.attribution, latest.url
FROM checkpoints c
CROSS JOIN LATERAL (
    SELECT o.wait_seconds, o.is_open, o.source_published_at, o.fetched_at,
           s.attribution, s.url
    FROM observations o
    JOIN sources s ON s.code = o.source_code
    WHERE o.checkpoint_id = c.id
    ORDER BY o.fetched_at DESC
    LIMIT 1
) latest
"""

FAA_EVENTS_SQL = """
SELECT airport_iata, event_type, reason, avg_delay_seconds, start_time, end_time, update_time
FROM faa_airport_events
WHERE fetched_at = (
        SELECT max(fetched_at) FROM raw_payloads WHERE source_code = %s
      )
  AND fetched_at > now() - interval '20 minutes'
  AND (start_time IS NULL OR start_time <= now())
  AND (end_time IS NULL OR end_time >= now())
"""


def faa_event_dict(row: tuple, *, include_iata: bool = False) -> dict:
    airport_iata, event_type, reason, avg_delay_seconds, _start_time, end_time, update_time = row
    event = {
        "event_type": event_type,
        "reason": reason,
        "avg_delay_seconds": avg_delay_seconds,
        "end_time": iso(end_time),
        "update_time": iso(update_time),
    }
    if include_iata:
        event["iata"] = airport_iata
    return event


def faa_sort_key(event: dict) -> tuple[int, str]:
    return (-FAA_EVENT_SEVERITY.get(event["event_type"], 0), event.get("iata", ""))


async def summary_airports(cur) -> dict[str, dict]:
    await cur.execute("SELECT iata, name, city, state, lat, lon, hub FROM airports")
    airports = {
        r[0]: {"iata": r[0], "name": r[1], "city": r[2], "state": r[3],
               "lat": r[4], "lon": r[5], "hub": r[6], "live": False}
        for r in await cur.fetchall()
    }
    await cur.execute(LATEST_OBS_SQL)
    now = datetime.now(UTC)
    for iata, _name, lane, wait, is_open, pub_at, fetched_at, attribution, url in await cur.fetchall():
        airport = airports.get(iata)
        if airport is None:
            continue
        stale = (now - fetched_at).total_seconds() > STALE_SECONDS
        airport["live"] = True
        airport.setdefault("stale", True)
        airport["stale"] = airport["stale"] and stale
        airport.setdefault("source", attribution)
        airport.setdefault("source_url", url)
        key = {
            "standard": "max_wait_seconds",
            "precheck": "max_precheck_seconds",
        }.get(lane)
        if key and wait is not None and is_open and not stale and wait > airport.get(key, -1):
            airport[key] = wait
            if lane == "standard":
                airport["as_of"] = iso(pub_at or fetched_at)
        fetched_iso = iso(fetched_at)
        if fetched_iso and fetched_iso > (airport.get("last_fetch") or ""):
            airport["last_fetch"] = fetched_iso
    return airports


async def airport_detail(cur, iata: str) -> dict | None:
    await cur.execute("SELECT iata, name, city, state, lat, lon FROM airports WHERE iata = %s", (iata,))
    row = await cur.fetchone()
    if row is None:
        return None
    airport = {"iata": row[0], "name": row[1], "city": row[2], "state": row[3], "lat": row[4], "lon": row[5]}
    await cur.execute(AIRPORT_ALERTS_SQL, (iata,))
    alerts = sorted((alert_dict(r) for r in await cur.fetchall()), key=alert_sort_key)
    await cur.execute(
        """
        SELECT year, enplanements, national_rank, hub, source_name, source_url
        FROM airport_enplanements WHERE airport_iata = %s ORDER BY year DESC LIMIT 1
        """,
        (iata,),
    )
    enplanement_row = await cur.fetchone()
    airport["enplanements"] = (
        {
            "year": enplanement_row[0],
            "enplanements": enplanement_row[1],
            "rank": enplanement_row[2],
            "hub": enplanement_row[3],
            "source": enplanement_row[4],
            "source_url": enplanement_row[5],
        }
        if enplanement_row is not None else None
    )
    current_travel_period = await travel_period(cur)
    await cur.execute(
        """
        SELECT c.id, c.name, c.lane_type, latest.wait_seconds, latest.is_open,
               latest.source_published_at, latest.fetched_at, latest.attribution, latest.url
        FROM checkpoints c
        CROSS JOIN LATERAL (
            SELECT o.wait_seconds, o.is_open, o.source_published_at, o.fetched_at,
                   s.attribution, s.url
            FROM observations o
            JOIN sources s ON s.code = o.source_code
            WHERE o.checkpoint_id = c.id
            ORDER BY o.fetched_at DESC
            LIMIT 1
        ) latest
        WHERE c.airport_iata = %s
        ORDER BY c.name, c.lane_type
        """,
        (iata,),
    )
    checkpoints = []
    now = datetime.now(UTC)
    latest_checkpoints = await cur.fetchall()
    airport_latest = max(
        (latest[6] for latest in latest_checkpoints),
        default=None,
    )
    history_by_checkpoint: dict[int, list[list[int | None]]] = {}
    await cur.execute(
        """
        SELECT o.checkpoint_id,
               extract(epoch FROM date_trunc('minute', o.fetched_at))::bigint AS m,
               max(o.wait_seconds)
        FROM observations o
        JOIN checkpoints c ON c.id = o.checkpoint_id
        WHERE c.airport_iata = %s AND o.wait_seconds IS NOT NULL
          AND o.fetched_at > now() - interval '24 hours'
        GROUP BY o.checkpoint_id, 2
        ORDER BY o.checkpoint_id, 2
        """,
        (iata,),
    )
    for checkpoint_id, minute, wait in await cur.fetchall():
        history_by_checkpoint.setdefault(checkpoint_id, []).append([minute, wait])
    for cp_id, cp_name, lane, wait, is_open, pub_at, fetched_at, attribution, src_url in latest_checkpoints:
        if airport_latest is not None and (airport_latest - fetched_at).total_seconds() > 24 * 60 * 60:
            continue
        history = history_by_checkpoint.get(cp_id, [])
        checkpoints.append({
            "name": cp_name,
            "lane_type": lane,
            "wait_seconds": wait,
            "is_open": is_open,
            "published_at": iso(pub_at),
            "fetched_at": iso(fetched_at),
            "stale": (now - fetched_at).total_seconds() > STALE_SECONDS,
            "source": attribution,
            "source_url": src_url,
            "history": history,
        })
    await cur.execute(FAA_EVENTS_SQL + " AND airport_iata = %s", (FAA_SOURCE_CODE, iata))
    faa_events = [faa_event_dict(row) for row in await cur.fetchall()]
    faa_events.sort(key=faa_sort_key)
    return {
        "airport": airport,
        "checkpoints": checkpoints,
        "faa_events": faa_events,
        "faa_attribution": FAA_ATTRIBUTION,
        "weather_alerts": alerts,
        "travel_period": current_travel_period,
        "generated_at": iso(datetime.now(UTC)),
    }


async def source_health(cur) -> dict:
    await cur.execute(
        """
            SELECT s.code, h.last_success_at, h.last_attempt_at, h.last_error,
                   h.last_error_at, h.consecutive_failures
            FROM sources s LEFT JOIN poll_health h ON h.source_code = s.code
            ORDER BY s.code
            """
    )
    rows = await cur.fetchall()
    await cur.execute("SELECT count(*) FROM observations")
    obs_row = await cur.fetchone()
    assert obs_row is not None
    obs_count = obs_row[0]
    now = datetime.now(UTC)
    sources = [
        {
            "source": code,
            "last_success_at": iso(last_success),
            "last_attempt_at": iso(last_attempt),
            "last_error": error,
            "last_error_at": iso(error_at),
            "consecutive_failures": failures or 0,
            "healthy": last_success is not None and (now - last_success).total_seconds() < STALE_SECONDS,
        }
        for code, last_success, last_attempt, error, error_at, failures in rows
    ]
    all_ok = all(source["healthy"] for source in sources)
    return {"status": "ok" if all_ok else "degraded", "observations": obs_count, "sources": sources}
