"""Database pool and schema management."""
import asyncio
import json
import os

from psycopg_pool import AsyncConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/waits")

pool: AsyncConnectionPool | None = None

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

CREATE TABLE IF NOT EXISTS poll_health (
    source_code TEXT PRIMARY KEY REFERENCES sources(code),
    last_success_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    last_error_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tsa_throughput (
    date DATE NOT NULL,
    travelers BIGINT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date)
);
"""


async def init() -> None:
    global pool
    pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=8, open=False)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    await seed_airports()


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
