"""Polling loops: one task per source, plus a daily TSA throughput task."""
import asyncio
import json
import logging
import random
from typing import Any

import httpx

from . import db, weather_alerts
from .sources.adapters import SOURCES
from .sources.base import USER_AGENT, FetchResult, Source
from .tsa_throughput import fetch_tsa_throughput

log = logging.getLogger("poller")

MAX_BACKOFF = 900  # 15 min


class EmptyPollError(RuntimeError):
    """A fetch succeeded but yielded no observations."""


async def register_sources() -> None:
    assert db.pool is not None
    async with db.pool.connection() as conn:
        for s in [*SOURCES, weather_alerts.SOURCE]:
            await conn.execute(
                """
                INSERT INTO sources (code, name, url, attribution, refresh_seconds)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET
                    name = EXCLUDED.name, url = EXCLUDED.url,
                    attribution = EXCLUDED.attribution,
                    refresh_seconds = EXCLUDED.refresh_seconds
                """,
                (s.code, s.name, s.url, s.attribution, s.refresh_seconds),
            )


async def store_result(
    source: Source, result: FetchResult, *, mark_success: bool = True
) -> None:
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO raw_payloads (source_code, payload) VALUES (%s, %s) RETURNING id",
            (source.code, json.dumps(result.raw, default=str)),
        )
        row = await cur.fetchone()
        raw_id = row[0] if row else None
        for ob in result.observations:
            await cur.execute(
                """
                INSERT INTO checkpoints (airport_iata, name, lane_type)
                VALUES (%s, %s, %s)
                ON CONFLICT (airport_iata, name, lane_type) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                (source.code, ob.checkpoint_name, ob.lane_type),
            )
            cp_row = await cur.fetchone()
            assert cp_row is not None
            await cur.execute(
                """
                INSERT INTO observations
                    (checkpoint_id, wait_seconds, is_open, source_code, source_published_at, raw_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (cp_row[0], ob.wait_seconds, ob.is_open, source.code, ob.published_at, raw_id),
            )
        if mark_success:
            await cur.execute(
                """
                INSERT INTO poll_health (source_code, last_success_at, last_attempt_at, consecutive_failures)
                VALUES (%s, now(), now(), 0)
                ON CONFLICT (source_code) DO UPDATE SET
                    last_success_at = now(), last_attempt_at = now(), consecutive_failures = 0
                """,
                (source.code,),
            )


async def mark_poll_success(source: Source) -> None:
    assert db.pool is not None
    async with db.pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO poll_health (source_code, last_success_at, last_attempt_at, consecutive_failures)
            VALUES (%s, now(), now(), 0)
            ON CONFLICT (source_code) DO UPDATE SET
                last_success_at = now(), last_attempt_at = now(),
                last_error = NULL, last_error_at = NULL, consecutive_failures = 0
            """,
            (source.code,),
        )


async def record_failure(source: Source, err: Exception) -> None:
    assert db.pool is not None
    async with db.pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO poll_health (source_code, last_attempt_at, last_error, last_error_at, consecutive_failures)
            VALUES (%s, now(), %s, now(), 1)
            ON CONFLICT (source_code) DO UPDATE SET
                last_attempt_at = now(), last_error = EXCLUDED.last_error,
                last_error_at = now(),
                consecutive_failures = poll_health.consecutive_failures + 1
            """,
            (source.code, f"{type(err).__name__}: {err}"[:500]),
        )


async def poll_source(source: Source, client: httpx.AsyncClient) -> None:
    # Stagger startup so sources don't all fire at once.
    await asyncio.sleep(random.uniform(0, 15))
    failures = 0
    while True:
        try:
            result = await source.fetch(client)
            await store_result(source, result, mark_success=bool(result.observations))
            if not result.observations:
                raise EmptyPollError(f"{source.code}: feed returned no observations")
            failures = 0
            log.info("polled %s: %d observations", source.code, len(result.observations))
        except Exception as err:  # noqa: BLE001 - keep the loop alive no matter what
            failures += 1
            log.warning("poll %s failed (%d): %s", source.code, failures, err)
            try:
                await record_failure(source, err)
            except Exception:
                log.exception("failed to record failure for %s", source.code)
        delay = source.refresh_seconds
        if failures:
            delay = min(source.refresh_seconds * (2 ** min(failures, 4)), MAX_BACKOFF)
        await asyncio.sleep(delay + random.uniform(0, 5))


async def poll_tsa_throughput(client: httpx.AsyncClient) -> None:
    while True:
        try:
            rows = await fetch_tsa_throughput(client)
            assert db.pool is not None
            async with db.pool.connection() as conn:
                for date, travelers in rows:
                    await conn.execute(
                        """
                        INSERT INTO tsa_throughput (date, travelers)
                        VALUES (%s, %s)
                        ON CONFLICT (date) DO UPDATE SET travelers = EXCLUDED.travelers, fetched_at = now()
                        """,
                        (date, travelers),
                    )
            log.info("tsa throughput: stored %d rows", len(rows))
            await asyncio.sleep(6 * 3600)
        except Exception as err:  # noqa: BLE001
            log.warning("tsa throughput failed: %s", err)
            await asyncio.sleep(1800)


async def poll_weather_alerts(client: httpx.AsyncClient) -> None:
    """One national api.weather.gov request per cycle, matched to cached airport zones."""
    source = weather_alerts.SOURCE
    failures = 0
    while True:
        try:
            await weather_alerts.refresh_zone_cache(client)
            zones = await weather_alerts.load_zone_cache()
            if not zones:
                raise EmptyPollError(f"{source.code}: no airport NWS zones resolved yet")
            payload = await weather_alerts.fetch_alerts(client)
            alerts = weather_alerts.parse_alerts(payload)
            features = payload.get("features") if isinstance(payload, dict) else None
            if not features:
                # The national active-alert feed is never empty in practice; an
                # empty body means the feed, not the weather, is broken.
                raise EmptyPollError(f"{source.code}: feed returned no alert features")
            coords = await weather_alerts.load_coords()
            matched = weather_alerts.match_alerts(alerts, zones, coords)
            raw_id = await store_raw(source, _matched_raw(matched))
            stored = await weather_alerts.store_alerts(matched, raw_id)
            await mark_poll_success(source)
            failures = 0
            log.info(
                "polled %s: %d relevant alerts, %d airports affected (%d rows)",
                source.code, len(alerts), len(matched), stored,
            )
        except Exception as err:  # noqa: BLE001 - keep the loop alive no matter what
            failures += 1
            log.warning("poll %s failed (%d): %s", source.code, failures, err)
            try:
                await record_failure(source, err)
            except Exception:
                log.exception("failed to record failure for %s", source.code)
        delay = source.refresh_seconds
        if failures:
            delay = min(source.refresh_seconds * (2 ** min(failures, 4)), MAX_BACKOFF)
        await asyncio.sleep(delay + random.uniform(0, 5))


def _matched_raw(matched: dict[str, list[tuple[weather_alerts.Alert, str]]]) -> dict[str, Any]:
    """Provenance for the alerts we kept (the full national feed is ~1.5 MB/cycle)."""
    return {
        "source": weather_alerts.ALERTS_URL,
        "matched": {
            iata: [
                {"alert_id": a.alert_id, "event": a.event, "severity": a.severity,
                 "headline": a.headline, "expires": a.expires, "match_basis": basis}
                for a, basis in entries
            ]
            for iata, entries in matched.items()
        },
    }


async def store_raw(source: Source, payload: Any) -> int | None:
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO raw_payloads (source_code, payload) VALUES (%s, %s) RETURNING id",
            (source.code, json.dumps(payload, default=str)),
        )
        row = await cur.fetchone()
        return row[0] if row else None


_tasks: list[asyncio.Task] = []
_client: httpx.AsyncClient | None = None


async def start() -> None:
    global _client
    await register_sources()
    _client = httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=30,
        follow_redirects=True,
    )
    for s in SOURCES:
        _tasks.append(asyncio.create_task(poll_source(s, _client)))
    _tasks.append(asyncio.create_task(poll_tsa_throughput(_client)))
    _tasks.append(asyncio.create_task(poll_weather_alerts(_client)))


async def stop() -> None:
    global _client
    for t in _tasks:
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    if _client is not None:
        await _client.aclose()
        _client = None
