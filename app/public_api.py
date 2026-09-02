"""Versioned public API and embeddable airport widget."""
import html
import time
from datetime import UTC, datetime
from math import ceil

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import db, queries, security

DATA_NOTICE = (
    "Wait times are aggregated from officially published public data sources; "
    "see attribution and source URLs in each record."
)
RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 60
MAX_TRACKED_CLIENTS = 10_000
_rate_limits: dict[str, tuple[float, int]] = {}
_last_prune: float = 0.0

v1_app = FastAPI(title="Wait Picture Public API v1")


@v1_app.exception_handler(404)
async def not_found_handler(_request: Request, exc: HTTPException):
    return JSONResponse(
        {"detail": exc.detail, "data_notice": DATA_NOTICE},
        status_code=404,
    )


def reset_rate_limiter() -> None:
    global _last_prune
    _rate_limits.clear()
    _last_prune = 0.0


def check_rate_limit(client_ip: str) -> int | None:
    """Count one request for ``client_ip``; return Retry-After seconds when over the limit."""
    global _last_prune
    now = time.monotonic()
    if now - _last_prune >= RATE_WINDOW_SECONDS:
        cutoff = now - RATE_WINDOW_SECONDS
        expired = [
            key
            for key, (window_start, _count) in _rate_limits.items()
            if window_start < cutoff
        ]
        for key in expired:
            del _rate_limits[key]
        _last_prune = now
    if client_ip not in _rate_limits and len(_rate_limits) >= MAX_TRACKED_CLIENTS:
        cutoff = now - RATE_WINDOW_SECONDS
        expired = [
            key
            for key, (window_start, _count) in _rate_limits.items()
            if window_start < cutoff
        ]
        for key in expired:
            del _rate_limits[key]
        if len(_rate_limits) >= MAX_TRACKED_CLIENTS:
            candidates = [
                key for key, (_ws, count) in _rate_limits.items() if count < RATE_LIMIT
            ] or list(_rate_limits)
            oldest = min(candidates, key=lambda key: _rate_limits[key][0])
            del _rate_limits[oldest]
    window_start, count = _rate_limits.get(client_ip, (now, 0))
    if now - window_start >= RATE_WINDOW_SECONDS:
        window_start, count = now, 0
    if count >= RATE_LIMIT:
        return max(1, ceil(window_start + RATE_WINDOW_SECONDS - now))
    _rate_limits[client_ip] = (window_start, count + 1)
    return None


@v1_app.middleware("http")
async def rate_limit(request: Request, call_next):
    retry_after = check_rate_limit(security.client_ip(request))
    if retry_after is not None:
        return JSONResponse(
            {"detail": "rate limit exceeded", "data_notice": DATA_NOTICE},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)


v1_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)


async def load_airports() -> list[dict]:
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        airports = await queries.summary_airports(cur)
    return [
        {
            "iata": airport["iata"],
            "name": airport["name"],
            "city": airport["city"],
            "state": airport["state"],
            "standard_wait_seconds": airport.get("max_wait_seconds"),
            "precheck_wait_seconds": airport.get("max_precheck_seconds"),
            "as_of": airport.get("as_of"),
            "last_updated": airport.get("last_fetch"),
            "stale": airport.get("stale", True),
            "attribution": airport.get("source"),
            "source_url": airport.get("source_url"),
        }
        for airport in airports.values()
        if airport["live"]
    ]


async def load_airport(iata: str) -> dict | None:
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        return await queries.airport_detail(cur, iata)


async def load_status() -> dict:
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        return await queries.source_health(cur)


async def load_embed(iata: str) -> dict | None:
    return await load_airport(iata)


def _now_iso() -> str:
    return queries.iso(datetime.now(UTC)) or ""


def _display_wait(checkpoints: list[dict], lane_type: str) -> str:
    waits = [
        checkpoint["wait_seconds"]
        for checkpoint in checkpoints
        if checkpoint.get("lane_type") == lane_type
        and checkpoint.get("is_open")
        and not checkpoint.get("stale")
        and checkpoint.get("wait_seconds") is not None
    ]
    if not waits:
        return "—"
    return str((max(waits) + 30) // 60)


def _updated_time(detail: dict) -> str:
    fetched = [
        checkpoint.get("fetched_at")
        for checkpoint in detail.get("checkpoints", [])
        if checkpoint.get("fetched_at")
        and checkpoint.get("is_open")
        and not checkpoint.get("stale")
        and checkpoint.get("wait_seconds") is not None
    ]
    value = max(fetched, default=detail.get("generated_at"))
    if not value:
        return "—"
    try:
        updated = datetime.fromisoformat(value).astimezone(UTC)
    except (TypeError, ValueError):
        return str(value)
    return updated.strftime("%H:%M UTC")


@v1_app.get("/airports")
async def airports():
    return JSONResponse({
        "generated_at": _now_iso(),
        "data_notice": DATA_NOTICE,
        "airports": await load_airports(),
    })


@v1_app.get("/airport/{iata}")
async def airport(iata: str, request: Request):
    code = security.validate_iata(iata, request)
    if code is None:
        return JSONResponse(
            {"detail": "unknown airport", "data_notice": DATA_NOTICE},
            status_code=404,
        )
    detail = await load_airport(code)
    if detail is None:
        return JSONResponse(
            {"detail": "unknown airport", "data_notice": DATA_NOTICE},
            status_code=404,
        )
    return JSONResponse({**detail, "data_notice": DATA_NOTICE})


@v1_app.get("/status")
async def status():
    return JSONResponse({
        "generated_at": _now_iso(),
        "data_notice": DATA_NOTICE,
        **await load_status(),
    })


async def embed_response(iata: str, request: Request) -> Response:
    retry_after = check_rate_limit(security.client_ip(request))
    if retry_after is not None:
        return JSONResponse(
            {"detail": "rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    code = security.validate_iata(iata, request)
    if code is None:
        return JSONResponse({"detail": "unknown airport"}, status_code=404)
    detail = await load_embed(code)
    if detail is None:
        return JSONResponse({"detail": "unknown airport"}, status_code=404)
    airport_data = detail["airport"]
    airport_iata = html.escape(str(airport_data["iata"]))
    airport_name = html.escape(str(airport_data["name"]))
    standard = html.escape(_display_wait(detail.get("checkpoints", []), "standard"))
    precheck = html.escape(_display_wait(detail.get("checkpoints", []), "precheck"))
    updated = html.escape(_updated_time(detail))
    powered_iata = html.escape(str(airport_data["iata"]))
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{airport_iata} security wait times</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 14px; font: 14px/1.35 system-ui, sans-serif; color: #17202a; background: #fff; }}
  .card {{ width: 100%; max-width: 292px; }}
  h1 {{ margin: 0 0 10px; font-size: 18px; font-weight: 650; }}
  .iata {{ color: #52616b; font-size: 12px; letter-spacing: .08em; }}
  .waits {{ display: flex; gap: 18px; margin: 8px 0 10px; }}
  .label {{ color: #52616b; font-size: 12px; }}
  .value {{ font-size: 22px; font-weight: 650; }}
  .updated {{ color: #52616b; font-size: 11px; }}
  a {{ color: #3969ca; font-size: 11px; }}
</style>
</head>
<body><main class="card">
  <h1>{airport_name} <span class="iata">{airport_iata}</span></h1>
  <div class="waits">
    <div><div class="label">Standard</div><div class="value">{standard} min</div></div>
    <div><div class="label">PreCheck</div><div class="value">{precheck} min</div></div>
  </div>
  <div class="updated">Updated {updated}</div>
  <a href="https://waitpicture.com/airport/{powered_iata}">powered by waitpicture.com</a>
</main></body>
</html>""",
    )
