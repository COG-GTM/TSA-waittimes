"""US Checkpoint Wait Picture — web app and API."""
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import analytics, db, forecast, leaderboard, poller, public_api, queries, security
from .faa_events import FAA_ATTRIBUTION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

CANONICAL_HOST = "waitpicture.com"
REDIRECT_HOSTS = frozenset({"tsadelays.com", "www.tsadelays.com", "www.waitpicture.com"})


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
app.mount("/api/v1", public_api.v1_app)
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


app.middleware("http")(security.security_middleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logging.getLogger(__name__).exception("unhandled error serving %s", request.url.path)
    return security.apply_security_headers(
        request, JSONResponse(security.GENERIC_ERROR, status_code=500)
    )


TYPICAL_HOURS_SQL = """
SELECT hour_bucket, max(avg_wait_seconds)::int AS value_seconds, sum(sample_count)::int
FROM observations_hourly
WHERE airport_iata = %s AND lane_type = 'standard' AND avg_wait_seconds IS NOT NULL AND sample_count > 0
GROUP BY 1
ORDER BY 1
"""
# Hour-of-week is UTC-based because the airports table carries no timezone
# (data/us_airports.json has iata/name/city/state/lat/lon/hub only).


async def _typical_buckets(cur, iata: str) -> list[analytics.TypicalBucket]:
    await cur.execute(TYPICAL_HOURS_SQL, (iata,))
    return analytics.typical_from_hours(await cur.fetchall())


async def _airport_name(cur, iata: str) -> tuple[str, str] | None:
    await cur.execute("SELECT iata, name FROM airports WHERE iata = %s", (iata,))
    row = await cur.fetchone()
    return (row[0], row[1]) if row is not None else None


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



@app.get("/api/leaderboard")
async def api_leaderboard():
    assert db.pool is not None
    now = datetime.now(UTC)
    target = now - leaderboard.BASELINE_AGE
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT iata, name FROM airports")
        names = {r[0]: r[1] for r in await cur.fetchall()}
        await cur.execute(queries.LATEST_OBS_SQL)
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
        airports = await queries.summary_airports(cur)
        await cur.execute(queries.ACTIVE_ALERTS_SQL)
        for row in await cur.fetchall():
            a = airports.get(row[0])
            if a is None:
                continue
            alert = queries.alert_dict(row)
            current = a.get("weather_alert")
            if current is None or queries.alert_sort_key(alert) < queries.alert_sort_key(current):
                a["weather_alert"] = alert
        travel_period = await queries.travel_period(cur)
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
        await cur.execute(queries.FAA_EVENTS_SQL, (queries.FAA_SOURCE_CODE,))
        faa_events = [queries.faa_event_dict(row, include_iata=True) for row in await cur.fetchall()]
    live = sum(1 for a in airports.values() if a["live"])
    faa_events.sort(key=queries.faa_sort_key)
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
        "generated_at": queries.iso(datetime.now(UTC)),
        "live_count": live,
        "no_data_count": len(airports) - live,
        "airports": list(airports.values()),
        "tsa_throughput": tsa,
        "faa_events": faa_events,
        "faa_attribution": FAA_ATTRIBUTION,
        "travel_period": travel_period,
    })


@app.get("/api/airport/{iata}")
async def api_airport(iata: str, request: Request):
    iata = security.require_iata(iata, request)
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        detail = await queries.airport_detail(cur, iata)
        if detail is not None:
            typical_buckets = await _typical_buckets(cur, iata)
            now = datetime.now(UTC)
            typical_hour = next(
                bucket for bucket in typical_buckets
                if bucket.dow == now.weekday() and bucket.hour == now.hour
            )
            current_minutes = analytics.seconds_to_minutes(
                max(
                    (
                        checkpoint["wait_seconds"]
                        for checkpoint in detail["checkpoints"]
                        if checkpoint["lane_type"] == "standard"
                        and checkpoint["is_open"]
                        and not checkpoint["stale"]
                        and checkpoint["wait_seconds"] is not None
                    ),
                    default=None,
                )
            )
            median_minutes = analytics.seconds_to_minutes(typical_hour.median_seconds)
            delta_minutes = (
                round(current_minutes - median_minutes, 1)
                if current_minutes is not None and median_minutes is not None else None
            )
            detail["typical"] = {
                "dow": typical_hour.dow,
                "hour": typical_hour.hour,
                "timezone": "UTC",
                "median_minutes": median_minutes,
                "p75_minutes": analytics.seconds_to_minutes(typical_hour.p75_seconds),
                "sample_count": typical_hour.sample_count,
                "current_minutes": current_minutes,
                "delta_minutes": delta_minutes,
            }
    if detail is None:
        raise HTTPException(404, "unknown airport")
    return JSONResponse(detail)


@app.get("/api/airport/{iata}/typical")
async def api_airport_typical(iata: str, request: Request):
    iata = security.require_iata(iata, request)
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        airport_row = await _airport_name(cur, iata)
        if airport_row is None:
            raise HTTPException(404, "unknown airport")
        buckets = await _typical_buckets(cur, iata)
    payload_buckets = [
        {
            "dow": bucket.dow,
            "hour": bucket.hour,
            "median_minutes": analytics.seconds_to_minutes(bucket.median_seconds),
            "p75_minutes": analytics.seconds_to_minutes(bucket.p75_seconds),
            "sample_count": bucket.sample_count,
            "observation_count": bucket.observation_count,
        }
        for bucket in buckets
    ]
    return JSONResponse({
        "airport": {"iata": airport_row[0], "name": airport_row[1]},
        "lane_type": "standard",
        "timezone": "UTC",
        "buckets": payload_buckets,
        "coverage": {
            "buckets_with_data": sum(bucket.sample_count > 0 for bucket in buckets),
            "total_buckets": len(buckets),
            "hour_buckets": sum(bucket.sample_count for bucket in buckets),
        },
        "generated_at": queries.iso(datetime.now(UTC)),
    })


@app.get("/api/airport/{iata}/forecast")
async def api_airport_forecast(iata: str, request: Request):
    iata = security.require_iata(iata, request)
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT iata, name FROM airports WHERE iata = %s", (iata,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "unknown airport")
        payload = await forecast.get_forecast(cur, row[0], row[1])
    return JSONResponse(payload)


@app.get("/healthz")
async def healthz():
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        health = await queries.source_health(cur)
    return JSONResponse(health, status_code=200)


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/airport/{iata}")
async def airport_page(request: Request, iata: str):
    iata = security.require_iata(iata, request)
    return templates.TemplateResponse(request, "airport.html", {"iata": iata})


@app.get("/embed/{iata}", response_class=HTMLResponse)
async def embed(iata: str, request: Request):
    return await public_api.embed_response(iata, request)


@app.get("/api", response_class=HTMLResponse)
async def api_docs(request: Request):
    return templates.TemplateResponse(request, "api_docs.html")
