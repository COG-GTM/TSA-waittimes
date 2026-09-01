"""US Checkpoint Wait Picture — web app and API."""
import logging
import os
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, leaderboard, poller, weather_alerts
from .faa_events import FAA_ATTRIBUTION, FAA_SOURCE_CODE
from .travel_calendar import TravelPeriod, period_payload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

STALE_SECONDS = 30 * 60
CANONICAL_HOST = "waitpicture.com"
REDIRECT_HOSTS = frozenset({"tsadelays.com", "www.tsadelays.com", "www.waitpicture.com"})
FAA_EVENT_SEVERITY = {
    "ground_stop": 5,
    "closure": 4,
    "ground_delay": 3,
    "arrival_delay": 2,
    "departure_delay": 1,
}
EASTERN = ZoneInfo("America/New_York")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.init()
    await poller.start()
    yield
    await poller.stop()
    await db.close()


app = FastAPI(title="US Checkpoint Wait Picture", lifespan=lifespan)

BASE = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


@app.middleware("http")
async def canonical_host_redirect(request: Request, call_next):
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    if host in REDIRECT_HOSTS:
        target = f"https://{CANONICAL_HOST}{request.url.path}"
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=301)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
    )
    return resp


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


ALERT_COLUMNS = """
SELECT airport_iata, event, severity, headline, area_desc, sender_name, alert_url,
       effective, onset, expires, ends, fetched_at
FROM weather_alerts
"""
ACTIVE_ALERTS_SQL = ALERT_COLUMNS + "WHERE expires IS NULL OR expires > now()"
AIRPORT_ALERTS_SQL = ALERT_COLUMNS + "WHERE airport_iata = %s AND (expires IS NULL OR expires > now())"


def _alert_dict(row: Sequence[Any]) -> dict[str, Any]:
    (_iata, event, severity, headline, area_desc, sender, url,
     effective, onset, expires, ends, fetched_at) = row
    return {
        "event": event,
        "severity": severity,
        "headline": headline,
        "area": area_desc,
        "sender": sender,
        "url": url,
        "effective": _iso(effective),
        "onset": _iso(onset),
        "expires": _iso(expires),
        "ends": _iso(ends),
        "fetched_at": _iso(fetched_at),
        "source": weather_alerts.ATTRIBUTION,
        "source_url": weather_alerts.PUBLIC_PAGE,
    }


def _alert_sort_key(alert: dict[str, Any]) -> tuple[int, str]:
    return (-weather_alerts.SEVERITY_RANK.get(alert["severity"], 0), alert["event"])


def _today() -> date:
    return datetime.now(UTC).astimezone(EASTERN).date()


async def _travel_period(cur) -> dict | None:
    today = _today()
    await cur.execute(
        """
        SELECT name, start_date, end_date, intensity, note
        FROM travel_periods
        WHERE end_date >= %s
        ORDER BY start_date, name
        LIMIT 1
        """,
        (today,),
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
    return period_payload(period, today)


LATEST_OBS_SQL = """
SELECT DISTINCT ON (o.checkpoint_id)
    c.airport_iata, c.name, c.lane_type, o.wait_seconds, o.is_open,
    o.source_published_at, o.fetched_at, s.attribution, s.url
FROM observations o
JOIN checkpoints c ON c.id = o.checkpoint_id
JOIN sources s ON s.code = o.source_code
ORDER BY o.checkpoint_id, o.fetched_at DESC
"""

LEADERBOARD_BASELINE_SQL = """
SELECT airport_iata, max(wait_seconds)
FROM (
    SELECT DISTINCT ON (o.checkpoint_id) c.airport_iata, o.wait_seconds
    FROM observations o JOIN checkpoints c ON c.id = o.checkpoint_id
    WHERE c.lane_type = 'standard' AND o.is_open AND o.wait_seconds IS NOT NULL
      AND o.fetched_at >= %s AND o.fetched_at <= %s
    ORDER BY o.checkpoint_id, abs(extract(epoch FROM (o.fetched_at - %s)))
) nearest
GROUP BY 1
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


def _faa_event_dict(row: tuple, *, include_iata: bool = False) -> dict:
    airport_iata, event_type, reason, avg_delay_seconds, _start_time, end_time, update_time = row
    event = {
        "event_type": event_type,
        "reason": reason,
        "avg_delay_seconds": avg_delay_seconds,
        "end_time": _iso(end_time),
        "update_time": _iso(update_time),
    }
    if include_iata:
        event["iata"] = airport_iata
    return event


def _faa_sort_key(event: dict) -> tuple[int, str]:
    return (-FAA_EVENT_SEVERITY.get(event["event_type"], 0), event.get("iata", ""))


@app.get("/api/leaderboard")
async def api_leaderboard():
    assert db.pool is not None
    now = datetime.now(UTC)
    target = now - leaderboard.BASELINE_AGE
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT iata, name FROM airports")
        names = {r[0]: r[1] for r in await cur.fetchall()}
        await cur.execute(LATEST_OBS_SQL)
        latest = await cur.fetchall()
        await cur.execute(
            LEADERBOARD_BASELINE_SQL,
            (
                target - leaderboard.BASELINE_TOLERANCE,
                target + leaderboard.BASELINE_TOLERANCE,
                target,
            ),
        )
        baseline = await cur.fetchall()
    return JSONResponse(leaderboard.build(latest, baseline, names, now=now))


@app.get("/api/summary")
async def api_summary():
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT iata, name, city, state, lat, lon, hub FROM airports")
        airports = {
            r[0]: {"iata": r[0], "name": r[1], "city": r[2], "state": r[3],
                   "lat": r[4], "lon": r[5], "hub": r[6], "live": False}
            for r in await cur.fetchall()
        }
        await cur.execute(LATEST_OBS_SQL)
        now = datetime.now(UTC)
        for iata, name, lane, wait, is_open, pub_at, fetched_at, attribution, url in await cur.fetchall():
            a = airports.get(iata)
            if a is None:
                continue
            stale = (now - fetched_at).total_seconds() > STALE_SECONDS
            a["live"] = True
            a.setdefault("stale", True)
            a["stale"] = a["stale"] and stale
            a.setdefault("source", attribution)
            a.setdefault("source_url", url)
            key = {
                "standard": "max_wait_seconds",
                "precheck": "max_precheck_seconds",
            }.get(lane)
            if key and wait is not None and is_open and not stale and wait > a.get(key, -1):
                a[key] = wait
                if lane == "standard":
                    a["as_of"] = _iso(pub_at or fetched_at)
            fetched_iso = _iso(fetched_at)
            if fetched_iso and fetched_iso > (a.get("last_fetch") or ""):
                a["last_fetch"] = fetched_iso
        await cur.execute(ACTIVE_ALERTS_SQL)
        for row in await cur.fetchall():
            a = airports.get(row[0])
            if a is None:
                continue
            alert = _alert_dict(row)
            current = a.get("weather_alert")
            if current is None or _alert_sort_key(alert) < _alert_sort_key(current):
                a["weather_alert"] = alert
        travel_period = await _travel_period(cur)
        await cur.execute(
            "SELECT date, travelers FROM tsa_throughput ORDER BY date DESC LIMIT 800"
        )
        tsa_rows = await cur.fetchall()
        await cur.execute(
            """
            SELECT date_trunc('week', date)::date AS wk, round(avg(travelers))::bigint
            FROM tsa_throughput
            WHERE date >= (SELECT max(date) FROM tsa_throughput) - interval '2 years'
            GROUP BY 1 HAVING count(*) >= 4 ORDER BY 1
            """
        )
        tsa_history = [[week.isoformat(), int(avg)] for week, avg in await cur.fetchall()]
        await cur.execute(FAA_EVENTS_SQL, (FAA_SOURCE_CODE,))
        faa_events = [_faa_event_dict(row, include_iata=True) for row in await cur.fetchall()]
    live = sum(1 for a in airports.values() if a["live"])
    faa_events.sort(key=_faa_sort_key)
    for event in faa_events:
        airport = airports.get(event["iata"])
        if airport is not None and "faa_event" not in airport:
            airport["faa_event"] = {key: value for key, value in event.items() if key != "iata"}
    tsa = None
    if tsa_rows:
        by_date = {d: t for d, t in tsa_rows}
        latest = max(d for d in by_date)
        try:
            lastyear_date = latest.replace(year=latest.year - 1)
        except ValueError:
            lastyear_date = None
        tsa = {
            "date": latest.isoformat(),
            "travelers": by_date[latest],
            "lastyear_date": lastyear_date.isoformat() if lastyear_date and lastyear_date in by_date else None,
            "lastyear_travelers": by_date.get(lastyear_date) if lastyear_date else None,
            "source": "TSA checkpoint travel numbers (tsa.gov/travel/passenger-volumes)",
            "history": tsa_history,
        }
    return JSONResponse({
        "generated_at": _iso(datetime.now(UTC)),
        "live_count": live,
        "no_data_count": len(airports) - live,
        "airports": list(airports.values()),
        "tsa_throughput": tsa,
        "faa_events": faa_events,
        "faa_attribution": FAA_ATTRIBUTION,
        "travel_period": travel_period,
    })


@app.get("/api/airport/{iata}")
async def api_airport(iata: str):
    iata = iata.upper()
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT iata, name, city, state, lat, lon FROM airports WHERE iata = %s", (iata,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "unknown airport")
        airport = {"iata": row[0], "name": row[1], "city": row[2], "state": row[3], "lat": row[4], "lon": row[5]}
        await cur.execute(AIRPORT_ALERTS_SQL, (iata,))
        alerts = sorted((_alert_dict(r) for r in await cur.fetchall()), key=_alert_sort_key)
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
        travel_period = await _travel_period(cur)
        await cur.execute(
            """
            SELECT c.id, c.name, c.lane_type FROM checkpoints c
            WHERE c.airport_iata = %s ORDER BY c.name, c.lane_type
            """,
            (iata,),
        )
        checkpoints = []
        now = datetime.now(UTC)
        latest_checkpoints = []
        for cp_id, cp_name, lane in await cur.fetchall():
            await cur.execute(
                """
                SELECT o.wait_seconds, o.is_open, o.source_published_at, o.fetched_at,
                       s.attribution, s.url
                FROM observations o JOIN sources s ON s.code = o.source_code
                WHERE o.checkpoint_id = %s ORDER BY o.fetched_at DESC LIMIT 1
                """,
                (cp_id,),
            )
            latest = await cur.fetchone()
            if latest is not None:
                latest_checkpoints.append((cp_id, cp_name, lane, latest))
        airport_latest = max(
            (latest[3] for _, _, _, latest in latest_checkpoints),
            default=None,
        )
        for cp_id, cp_name, lane, latest in latest_checkpoints:
            wait, is_open, pub_at, fetched_at, attribution, src_url = latest
            if airport_latest is not None and (airport_latest - fetched_at).total_seconds() > 24 * 60 * 60:
                continue
            await cur.execute(
                """
                SELECT extract(epoch FROM date_trunc('minute', fetched_at))::bigint AS m,
                       max(wait_seconds)
                FROM observations
                WHERE checkpoint_id = %s AND wait_seconds IS NOT NULL
                  AND fetched_at > now() - interval '24 hours'
                GROUP BY 1 ORDER BY 1
                """,
                (cp_id,),
            )
            history = [[m, w] for m, w in await cur.fetchall()]
            checkpoints.append({
                "name": cp_name,
                "lane_type": lane,
                "wait_seconds": wait,
                "is_open": is_open,
                "published_at": _iso(pub_at),
                "fetched_at": _iso(fetched_at),
                "stale": (now - fetched_at).total_seconds() > STALE_SECONDS,
                "source": attribution,
                "source_url": src_url,
                "history": history,
            })
        await cur.execute(FAA_EVENTS_SQL + " AND airport_iata = %s", (FAA_SOURCE_CODE, iata))
        faa_events = [_faa_event_dict(row) for row in await cur.fetchall()]
        faa_events.sort(key=_faa_sort_key)
    return JSONResponse({
        "airport": airport,
        "checkpoints": checkpoints,
        "faa_events": faa_events,
        "faa_attribution": FAA_ATTRIBUTION,
        "weather_alerts": alerts,
        "travel_period": travel_period,
        "generated_at": _iso(datetime.now(UTC)),
    })


@app.get("/healthz")
async def healthz():
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
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
            "last_success_at": _iso(ls),
            "last_attempt_at": _iso(la),
            "last_error": err,
            "last_error_at": _iso(ea),
            "consecutive_failures": cf or 0,
            "healthy": ls is not None and (now - ls).total_seconds() < STALE_SECONDS,
        }
        for code, ls, la, err, ea, cf in rows
    ]
    all_ok = all(s["healthy"] for s in sources)
    return JSONResponse(
        {"status": "ok" if all_ok else "degraded", "observations": obs_count, "sources": sources},
        status_code=200,
    )


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/airport/{iata}")
async def airport_page(request: Request, iata: str):
    return templates.TemplateResponse(request, "airport.html", {"iata": iata.upper()})
