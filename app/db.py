"""Database pool and schema management."""
import asyncio
import json
import logging
import os

from psycopg_pool import AsyncConnectionPool

from .enplanements import ENPLANEMENTS_PATH, iata_for_locid, load_enplanements
from .travel_calendar import load_periods

log = logging.getLogger("db")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/waits")

pool: AsyncConnectionPool | None = None

POOL_KWARGS = {
    "options": "-c timezone=UTC",
    "tcp_user_timeout": 15000,
    "keepalives": 1,
    "keepalives_idle": 5,
    "keepalives_interval": 2,
    "keepalives_count": 3,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS airports (
    iata TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT,
    state TEXT,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    hub TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    attribution TEXT NOT NULL,
    refresh_seconds INTEGER NOT NULL DEFAULT 120
);

CREATE TABLE IF NOT EXISTS raw_payloads (
    id BIGSERIAL PRIMARY KEY,
    source_code TEXT NOT NULL REFERENCES sources(code),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS faa_airport_events (
    id BIGSERIAL PRIMARY KEY,
    airport_iata TEXT NOT NULL REFERENCES airports(iata),
    event_type TEXT NOT NULL,
    reason TEXT,
    avg_delay_seconds INTEGER,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    update_time TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_id BIGINT REFERENCES raw_payloads(id)
);
CREATE INDEX IF NOT EXISTS idx_faa_events_time ON faa_airport_events (fetched_at DESC);

CREATE TABLE IF NOT EXISTS checkpoints (
    id SERIAL PRIMARY KEY,
    airport_iata TEXT NOT NULL REFERENCES airports(iata),
    name TEXT NOT NULL,
    lane_type TEXT NOT NULL DEFAULT 'standard',
    UNIQUE (airport_iata, name, lane_type)
);

CREATE TABLE IF NOT EXISTS observations (
    id BIGSERIAL PRIMARY KEY,
    checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(id),
    wait_seconds INTEGER,
    is_open BOOLEAN NOT NULL DEFAULT TRUE,
    source_code TEXT NOT NULL REFERENCES sources(code),
    source_published_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_id BIGINT REFERENCES raw_payloads(id)
);
CREATE INDEX IF NOT EXISTS idx_obs_checkpoint_time ON observations (checkpoint_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_time ON observations (fetched_at DESC);

CREATE TABLE IF NOT EXISTS observations_hourly (
    airport_iata TEXT NOT NULL REFERENCES airports(iata),
    checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(id),
    lane_type TEXT NOT NULL,
    hour_bucket TIMESTAMPTZ NOT NULL,
    avg_wait_seconds INTEGER,
    max_wait_seconds INTEGER,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (checkpoint_id, hour_bucket)
);
CREATE INDEX IF NOT EXISTS idx_obs_hourly_airport ON observations_hourly (airport_iata, lane_type, hour_bucket);

CREATE TABLE IF NOT EXISTS poll_health (
    source_code TEXT PRIMARY KEY REFERENCES sources(code),
    last_success_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    last_error_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

-- NWS forecast/county zone for each airport, resolved once via api.weather.gov/points.
CREATE TABLE IF NOT EXISTS airport_nws_zones (
    airport_iata TEXT PRIMARY KEY REFERENCES airports(iata),
    forecast_zone TEXT,
    county_zone TEXT,
    fire_zone TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Currently active NWS alerts matched to an airport; rewritten every poll cycle.
CREATE TABLE IF NOT EXISTS weather_alerts (
    airport_iata TEXT NOT NULL REFERENCES airports(iata),
    alert_id TEXT NOT NULL,
    event TEXT NOT NULL,
    severity TEXT NOT NULL,
    urgency TEXT,
    certainty TEXT,
    headline TEXT,
    area_desc TEXT,
    sender_name TEXT,
    alert_url TEXT,
    effective TIMESTAMPTZ,
    onset TIMESTAMPTZ,
    expires TIMESTAMPTZ,
    ends TIMESTAMPTZ,
    match_basis TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_id BIGINT REFERENCES raw_payloads(id),
    PRIMARY KEY (airport_iata, alert_id)
);
CREATE INDEX IF NOT EXISTS idx_weather_alerts_airport ON weather_alerts (airport_iata);

CREATE TABLE IF NOT EXISTS tsa_throughput (
    date DATE NOT NULL,
    travelers BIGINT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date)
);
CREATE TABLE IF NOT EXISTS airport_enplanements (
    airport_iata TEXT NOT NULL,
    year INTEGER NOT NULL,
    enplanements BIGINT NOT NULL,
    national_rank INTEGER,
    hub TEXT,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (airport_iata, year)
);

CREATE TABLE IF NOT EXISTS travel_periods (
    name TEXT NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    intensity TEXT NOT NULL CHECK (intensity IN ('elevated', 'peak')),
    note TEXT NOT NULL,
    PRIMARY KEY (name, start_date)
);
"""


async def init() -> None:
    global pool
    pool = AsyncConnectionPool(
        DATABASE_URL,
        kwargs=POOL_KWARGS,
        min_size=2,
        max_size=8,
        open=False,
    )
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    await seed_airports()
    await seed_enplanements()
    await seed_travel_periods()


async def close() -> None:
    if pool is not None:
        await pool.close()


def _load_json(path: str):
    with open(path) as f:
        return json.load(f)


async def seed_airports() -> None:
    path = os.path.join(os.path.dirname(__file__), "..", "data", "us_airports.json")
    airports = await asyncio.to_thread(_load_json, path)
    assert pool is not None
    async with pool.connection() as conn, conn.cursor() as cur:
        for a in airports:
            await cur.execute(
                """
                INSERT INTO airports (iata, name, city, state, lat, lon, hub)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (iata) DO UPDATE SET
                    name = EXCLUDED.name, city = EXCLUDED.city, state = EXCLUDED.state,
                    lat = EXCLUDED.lat, lon = EXCLUDED.lon, hub = EXCLUDED.hub
                """,
                (a["iata"], a["name"], a["city"], a["state"], a["lat"], a["lon"], a["hub"]),
            )
async def seed_enplanements() -> None:
    try:
        data = await asyncio.to_thread(load_enplanements)
        if data is None:
            log.warning("enplanements data file is missing: %s", ENPLANEMENTS_PATH)
            return
        year = data["year"]
        source_name = data["source_name"]
        source_url = data["source_url"]
        airports = data["airports"]
        if not isinstance(year, int) or isinstance(year, bool) or not isinstance(source_name, str) or not isinstance(source_url, str):
            raise TypeError("invalid enplanements metadata")
        if not isinstance(airports, list):
            raise TypeError("invalid enplanements airports")
        assert pool is not None
        async with pool.connection() as conn, conn.cursor() as cur:
            seeded_codes: list[str] = []
            for airport in airports:
                if not isinstance(airport, dict):
                    raise TypeError("invalid enplanements airport record")
                locid = airport["locid"]
                if not isinstance(locid, str):
                    raise TypeError("invalid enplanements airport locid")
                airport_iata = iata_for_locid(locid)
                seeded_codes.append(airport_iata)
                await cur.execute(
                    """
                    INSERT INTO airport_enplanements
                        (airport_iata, year, enplanements, national_rank, hub, source_name, source_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (airport_iata, year) DO UPDATE SET
                        enplanements = EXCLUDED.enplanements, national_rank = EXCLUDED.national_rank,
                        hub = EXCLUDED.hub, source_name = EXCLUDED.source_name, source_url = EXCLUDED.source_url
                    """,
                    (
                        airport_iata, year, airport["enplanements"], airport["rank"], airport["hub"],
                        source_name, source_url,
                    ),
                )
            await cur.execute(
                """
                DELETE FROM airport_enplanements
                WHERE year = %s AND airport_iata <> ALL(%s)
                """,
                (year, seeded_codes),
            )
    except Exception as err:  # noqa: BLE001 - invalid optional data must not break startup
        log.warning("could not seed enplanements: %s", err)


async def seed_travel_periods() -> None:
    path = os.path.join(os.path.dirname(__file__), "..", "data", "travel_calendar.json")
    periods = await asyncio.to_thread(load_periods, path)
    assert pool is not None
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM travel_periods")
        for period in periods:
            await cur.execute(
                """
                INSERT INTO travel_periods (name, start_date, end_date, intensity, note)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (period.name, period.start, period.end, period.intensity, period.note),
            )
