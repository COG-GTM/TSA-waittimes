"""US Checkpoint Wait Picture — web app and API."""
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, poller

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

STALE_SECONDS = 30 * 60
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
    return dt.astimezone(timezone.utc).isoformat() if dt else None


LATEST_OBS_SQL = """
SELECT DISTINCT ON (o.checkpoint_id)
    c.airport_iata, c.name, c.lane_type, o.wait_seconds, o.is_open,
    o.source_published_at, o.fetched_at, s.attribution, s.url
FROM observations o
JOIN checkpoints c ON c.id = o.checkpoint_id
JOIN sources s ON s.code = o.source_code
ORDER BY o.checkpoint_id, o.fetched_at DESC
"""


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
        now = datetime.now(timezone.utc)
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
            if wait is not None and is_open and not stale:
                key = "max_wait_seconds" if lane == "standard" else "max_precheck_seconds"
                if wait > a.get(key, -1):
                    a[key] = wait
                    if lane == "standard":
                        a["as_of"] = _iso(pub_at or fetched_at)
            fetched_iso = _iso(fetched_at)
            if fetched_iso and fetched_iso > (a.get("last_fetch") or ""):
                a["last_fetch"] = fetched_iso
        await cur.execute(
            "SELECT date, travelers FROM tsa_throughput ORDER BY date DESC LIMIT 800"
        )
        tsa_rows = await cur.fetchall()
    live = sum(1 for a in airports.values() if a["live"])
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
        }
    return JSONResponse({
        "generated_at": _iso(datetime.now(timezone.utc)),
        "live_count": live,
        "no_data_count": len(airports) - live,
        "airports": list(airports.values()),
        "tsa_throughput": tsa,
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
        await cur.execute(
            """
            SELECT c.id, c.name, c.lane_type FROM checkpoints c
            WHERE c.airport_iata = %s ORDER BY c.name, c.lane_type
            """,
            (iata,),
        )
        checkpoints = []
        now = datetime.now(timezone.utc)
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
            if latest is None:
                continue
            wait, is_open, pub_at, fetched_at, attribution, src_url = latest
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
    return JSONResponse({"airport": airport, "checkpoints": checkpoints,
                         "generated_at": _iso(datetime.now(timezone.utc))})


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
        obs_count = (await cur.fetchone())[0]
    now = datetime.now(timezone.utc)
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
