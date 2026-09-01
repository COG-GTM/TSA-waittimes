"""US Checkpoint Wait Picture — web app and API."""
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, poller, public_api, queries
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


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    is_embed = request.url.path.startswith("/embed/")
    if not is_embed:
        resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors *"
        if is_embed else
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
    )
    return resp


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
async def api_airport(iata: str):
    iata = iata.upper()
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        detail = await queries.airport_detail(cur, iata)
    if detail is None:
        raise HTTPException(404, "unknown airport")
    return JSONResponse(detail)


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
    return templates.TemplateResponse(request, "airport.html", {"iata": iata.upper()})


@app.get("/embed/{iata}", response_class=HTMLResponse)
async def embed(iata: str):
    return await public_api.embed_response(iata)


@app.get("/api", response_class=HTMLResponse)
async def api_docs(request: Request):
    return templates.TemplateResponse(request, "api_docs.html")
