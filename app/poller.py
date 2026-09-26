"""Polling loops: one task per source, plus a daily TSA throughput task."""
import asyncio
import calendar
import json
import logging
import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from . import analytics, db, weather_alerts
from .faa_events import (
    FAA_ATTRIBUTION,
    FAA_PUBLIC_URL,
    FAA_SOURCE_CODE,
    REFRESH_SECONDS,
    fetch_faa_events,
)
from .sources.adapters import SOURCES
from .sources.base import USER_AGENT, FetchResult, Source
from .sources.credentials import missing_feed_credentials
from .tsa_throughput import FIRST_YEAR, fetch_tsa_throughput, fetch_tsa_year

log = logging.getLogger("poller")

MAX_BACKOFF = 900  # 15 min
LAST_CLEANUP_AT: datetime | None = None
FAA_SOURCE = Source(
    FAA_SOURCE_CODE,
    "FAA National Airspace System Status",
    FAA_PUBLIC_URL,
    FAA_ATTRIBUTION,
    REFRESH_SECONDS,
    fetch_faa_events,
)


class EmptyPollError(RuntimeError):
    """A fetch succeeded but yielded no observations."""


async def register_sources() -> None:
    assert db.pool is not None
    async with db.pool.connection() as conn:
        for s in (*SOURCES, FAA_SOURCE, weather_alerts.SOURCE):
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
                    last_success_at = now(), last_attempt_at = now(),
                    last_error = NULL, last_error_at = NULL, consecutive_failures = 0
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
            await upsert_tsa_throughput(rows)
            log.info("tsa throughput: stored %d rows", len(rows))
            await asyncio.sleep(6 * 3600)
        except Exception as err:  # noqa: BLE001
            log.warning("tsa throughput failed: %s", err)
            await asyncio.sleep(1800)


async def upsert_tsa_throughput(rows: list[tuple[date, int]]) -> None:
    assert db.pool is not None
    async with db.pool.connection() as conn:
        for row_date, travelers in rows:
            await conn.execute(
                """
                INSERT INTO tsa_throughput (date, travelers)
                VALUES (%s, %s)
                ON CONFLICT (date) DO UPDATE SET travelers = EXCLUDED.travelers, fetched_at = now()
                """,
                (row_date, travelers),
            )


async def backfill_tsa_throughput() -> None:
    """Backfill missing historical TSA years without affecting startup."""
    try:
        await asyncio.sleep(10)
        assert db.pool is not None
        async with db.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT extract(year from date)::int, count(*) FROM tsa_throughput GROUP BY 1"
            )
            existing = {year: count for year, count in await cur.fetchall()}
        current_year = datetime.now(UTC).year
        for year in range(FIRST_YEAR, current_year):
            if existing.get(year, 0) >= (366 if calendar.isleap(year) else 365) - 5:
                continue
            try:
                rows = await fetch_tsa_year(year)
                await upsert_tsa_throughput(rows)
                log.info("backfilled tsa throughput %d: %d rows", year, len(rows))
                await asyncio.sleep(3)
            except Exception as err:  # noqa: BLE001 - one blocked year must not stop the rest
                log.warning("backfill tsa throughput %d failed: %s", year, err)
    except Exception as err:  # noqa: BLE001 - backfill must never crash startup
        log.warning("tsa throughput backfill failed: %s", err)


async def poll_faa_events(client: httpx.AsyncClient) -> None:
    failures = 0
    while True:
        try:
            raw, events = await fetch_faa_events(client)
            fetched_at = datetime.now(UTC)
            assert db.pool is not None
            async with db.pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO raw_payloads (source_code, fetched_at, payload)
                    VALUES (%s, %s, %s) RETURNING id
                    """,
                    (FAA_SOURCE_CODE, fetched_at, json.dumps(raw, default=str)),
                )
                row = await cur.fetchone()
                raw_id = row[0] if row else None
                await cur.execute("SELECT iata FROM airports")
                airport_iatas = {row[0] for row in await cur.fetchall()}
                for event in events:
                    if event.airport_iata not in airport_iatas:
                        continue
                    await cur.execute(
                        """
                        INSERT INTO faa_airport_events
                            (airport_iata, event_type, reason, avg_delay_seconds,
                             start_time, end_time, update_time, fetched_at, raw_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            event.airport_iata,
                            event.event_type,
                            event.reason,
                            event.avg_delay_seconds,
                            event.start_time,
                            event.end_time,
                            event.update_time,
                            fetched_at,
                            raw_id,
                        ),
                    )
                await cur.execute(
                    """
                    INSERT INTO poll_health (source_code, last_success_at, last_attempt_at, consecutive_failures)
                    VALUES (%s, now(), now(), 0)
                    ON CONFLICT (source_code) DO UPDATE SET
                        last_success_at = now(), last_attempt_at = now(),
                        last_error = NULL, last_error_at = NULL, consecutive_failures = 0
                    """,
                    (FAA_SOURCE_CODE,),
                )
            failures = 0
            log.info("polled %s: %d events", FAA_SOURCE_CODE, len(events))
        except Exception as err:  # noqa: BLE001 - keep the loop alive no matter what
            failures += 1
            log.warning("poll %s failed (%d): %s", FAA_SOURCE_CODE, failures, err)
            try:
                await record_failure(FAA_SOURCE, err)
            except Exception:
                log.exception("failed to record failure for %s", FAA_SOURCE_CODE)
        delay = REFRESH_SECONDS
        if failures:
            delay = min(REFRESH_SECONDS * (2 ** min(failures, 4)), MAX_BACKOFF)
        await asyncio.sleep(delay + random.uniform(0, 5))


class ZoneBackfillIncomplete(RuntimeError):
    """Some airports still have no cached NWS zone, so coverage is partial."""


async def poll_weather_alerts(client: httpx.AsyncClient) -> None:
    """One national api.weather.gov request per cycle, matched to cached airport zones."""
    source = weather_alerts.SOURCE
    failures = 0
    while True:
        try:
            await weather_alerts.refresh_zone_cache(client)
            zones = await weather_alerts.load_zone_cache()
            cached, total = await weather_alerts.zone_coverage()
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
            raw_id = await store_raw(source, _matched_raw(matched, zones))
            stored = await weather_alerts.store_alerts(matched, raw_id)
            log.info(
                "polled %s: %d relevant alerts, %d airports affected (%d rows)",
                source.code, len(alerts), len(matched), stored,
            )
            # Alerts for the zones we do know are published, but a partially
            # backfilled cache silently under-reports, so it is not a healthy poll.
            if cached < total:
                raise ZoneBackfillIncomplete(
                    f"{source.code}: NWS zones cached for {cached}/{total} airports"
                )
            await mark_poll_success(source)
            failures = 0
        except Exception as err:  # noqa: BLE001 - keep the loop alive no matter what
            # Backing off during the backfill would only delay reaching full
            # coverage, so an incomplete cache keeps the normal cadence.
            if not isinstance(err, ZoneBackfillIncomplete):
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


def _matched_raw(
    matched: dict[str, list[tuple[weather_alerts.Alert, str]]],
    zones: dict[str, weather_alerts.AirportZones],
) -> dict[str, Any]:
    """Provenance for the alerts we kept (the full national feed is ~1.5 MB/cycle).

    Records both sides of the match — the alert's zones/geometry and the airport's
    cached zones — so a stored row can be re-derived after the upstream product,
    or our matching, changes.
    """
    return {
        "source": weather_alerts.ALERTS_URL,
        "matched": {
            iata: {
                "airport_zones": sorted(zones[iata].codes) if iata in zones else [],
                "alerts": [
                    {"alert_id": a.alert_id, "event": a.event, "severity": a.severity,
                     "headline": a.headline, "expires": a.expires, "match_basis": basis,
                     "alert_zones": sorted(a.zones),
                     "geometry": [
                         {"outer": outer, "holes": holes} for outer, holes in a.polygons
                     ]}
                    for a, basis in entries
                ],
            }
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


async def roll_up_hours(hours: Sequence[datetime]) -> int:
    assert db.pool is not None
    observations: list[analytics.ObservationRow] = []
    async with db.pool.connection() as conn, conn.cursor() as cur:
        for hour in hours:
            await cur.execute(
                """
                SELECT c.airport_iata, o.checkpoint_id, c.lane_type, o.fetched_at,
                       -- Closed lanes are aggregated as null waits, not zeros.
                       CASE WHEN o.is_open THEN o.wait_seconds END
                FROM observations o
                JOIN checkpoints c ON c.id = o.checkpoint_id
                WHERE o.fetched_at >= %s AND o.fetched_at < %s
                """,
                (hour, hour + timedelta(hours=1)),
            )
            observations.extend(
                analytics.ObservationRow(
                    airport_iata=airport_iata,
                    checkpoint_id=checkpoint_id,
                    lane_type=lane_type,
                    fetched_at=fetched_at,
                    wait_seconds=wait_seconds,
                )
                for airport_iata, checkpoint_id, lane_type, fetched_at, wait_seconds in await cur.fetchall()
            )
        rollups = analytics.aggregate_observations(observations)
        if rollups:
            await cur.executemany(
                """
                INSERT INTO observations_hourly
                    (airport_iata, checkpoint_id, lane_type, hour_bucket,
                     avg_wait_seconds, max_wait_seconds, sample_count, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (checkpoint_id, hour_bucket) DO UPDATE SET
                    airport_iata = EXCLUDED.airport_iata,
                    lane_type = EXCLUDED.lane_type,
                    avg_wait_seconds = EXCLUDED.avg_wait_seconds,
                    max_wait_seconds = EXCLUDED.max_wait_seconds,
                    sample_count = EXCLUDED.sample_count,
                    updated_at = now()
                """,
                [
                    (
                        row.airport_iata,
                        row.checkpoint_id,
                        row.lane_type,
                        row.hour_bucket,
                        row.avg_wait_seconds,
                        row.max_wait_seconds,
                        row.sample_count,
                    )
                    for row in rollups
                ],
            )
    return len(rollups)


async def poll_rollups() -> None:
    while True:
        try:
            current_hour = analytics.hour_bucket(datetime.now(UTC))
            first_hour = current_hour - timedelta(hours=analytics.ROLLUP_LOOKBACK_HOURS - 1)
            hours = [
                first_hour + timedelta(hours=offset)
                for offset in range(analytics.ROLLUP_LOOKBACK_HOURS)
            ]
            written = await roll_up_hours(hours)
            log.info("rollup: %d rows across %d hours", written, len(hours))
            await asyncio.sleep(analytics.ROLLUP_INTERVAL_SECONDS)
        except Exception as err:  # noqa: BLE001 - keep the loop alive
            log.warning("rollup failed: %s", err)
            await asyncio.sleep(300)


async def backfill_rollups() -> None:
    """Backfill historical hourly rollups without affecting startup."""
    try:
        await asyncio.sleep(15)
        assert db.pool is not None
        async with db.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT date_trunc('hour', fetched_at) AS hb, count(DISTINCT checkpoint_id)
                FROM observations
                GROUP BY 1 ORDER BY 1
                """
            )
            observation_counts: dict[datetime, int] = dict(await cur.fetchall())
            await cur.execute(
                """
                SELECT hour_bucket, count(*), min(updated_at)
                FROM observations_hourly
                GROUP BY 1
                """
            )
            rollup_counts: dict[datetime, tuple[int, datetime | None]] = {
                hour: (count, min_updated_at)
                for hour, count, min_updated_at in await cur.fetchall()
            }

        current_hour = analytics.hour_bucket(datetime.now(UTC))
        hours = []
        for hour, checkpoint_count in observation_counts.items():
            rollup_count, min_updated_at = rollup_counts.get(hour, (0, None))
            if (
                hour >= current_hour
                or rollup_count < checkpoint_count
                or (
                    min_updated_at is not None
                    and min_updated_at < hour + timedelta(hours=1)
                )
            ):
                hours.append(hour)
        written = 0
        batches = 0
        for start in range(0, len(hours), 6):
            batch = hours[start:start + 6]
            written += await roll_up_hours(batch)
            batches += 1
            if start + 6 < len(hours):
                await asyncio.sleep(1)
        log.info("rollup backfill: %d rows across %d hours in %d batches", written, len(hours), batches)
    except Exception as err:  # noqa: BLE001 - backfill must never crash startup
        log.warning("rollup backfill failed: %s", err)


async def cleanup_once(now: datetime | None = None) -> dict[str, Any]:
    global LAST_CLEANUP_AT
    started = datetime.now(UTC)
    cleanup_now = now or started
    cutoffs = {
        "observations": analytics.retention_cutoff(cleanup_now, analytics.RETENTION_OBSERVATION_DAYS),
        "raw_payloads": analytics.retention_cutoff(cleanup_now, analytics.RETENTION_RAW_PAYLOAD_DAYS),
        "faa_airport_events": analytics.retention_cutoff(cleanup_now, analytics.RETENTION_FAA_EVENT_DAYS),
        "weather_alerts": analytics.retention_cutoff(cleanup_now, analytics.RETENTION_WEATHER_ALERT_DAYS),
    }
    deleted = {
        "observations": 0,
        "raw_payloads": 0,
        "faa_airport_events": 0,
        "weather_alerts": 0,
    }
    refs_cleared = 0
    assert db.pool is not None
    async with db.pool.connection() as conn, conn.cursor() as cur:
        for _ in range(20):
            await cur.execute(
                """
                DELETE FROM observations o WHERE o.ctid IN (
                    SELECT o2.ctid FROM observations o2
                    WHERE o2.fetched_at < %s
                      AND EXISTS (
                          SELECT 1 FROM observations_hourly h
                          WHERE h.checkpoint_id = o2.checkpoint_id
                            AND o2.fetched_at >= h.hour_bucket
                            AND o2.fetched_at < h.hour_bucket + interval '1 hour'
                            AND h.updated_at >= h.hour_bucket + interval '1 hour'
                      )
                    LIMIT %s
                )
                """,
                (cutoffs["observations"], analytics.CLEANUP_BATCH_LIMIT),
            )
            count = max(cur.rowcount, 0)
            deleted["observations"] += count
            await conn.commit()
            if count == 0:
                break

        for _ in range(20):
            await cur.execute(
                """
                DELETE FROM faa_airport_events e WHERE e.ctid IN (
                    SELECT e2.ctid FROM faa_airport_events e2
                    WHERE e2.fetched_at < %s
                    LIMIT %s
                )
                """,
                (cutoffs["faa_airport_events"], analytics.CLEANUP_BATCH_LIMIT),
            )
            count = max(cur.rowcount, 0)
            deleted["faa_airport_events"] += count
            await conn.commit()
            if count == 0:
                break

        for _ in range(20):
            await cur.execute(
                """
                DELETE FROM weather_alerts w WHERE w.ctid IN (
                    SELECT w2.ctid FROM weather_alerts w2
                    WHERE coalesce(w2.expires, w2.ends, w2.fetched_at) < %s
                    LIMIT %s
                )
                """,
                (cutoffs["weather_alerts"], analytics.CLEANUP_BATCH_LIMIT),
            )
            count = max(cur.rowcount, 0)
            deleted["weather_alerts"] += count
            await conn.commit()
            if count == 0:
                break

        for _ in range(20):
            await cur.execute(
                "SELECT id FROM raw_payloads WHERE fetched_at < %s LIMIT %s",
                (cutoffs["raw_payloads"], analytics.CLEANUP_BATCH_LIMIT),
            )
            ids = [row[0] for row in await cur.fetchall()]
            if not ids:
                break
            await cur.execute("UPDATE observations SET raw_id = NULL WHERE raw_id = ANY(%s)", (ids,))
            refs_cleared += max(cur.rowcount, 0)
            await cur.execute("UPDATE faa_airport_events SET raw_id = NULL WHERE raw_id = ANY(%s)", (ids,))
            refs_cleared += max(cur.rowcount, 0)
            await cur.execute("UPDATE weather_alerts SET raw_id = NULL WHERE raw_id = ANY(%s)", (ids,))
            refs_cleared += max(cur.rowcount, 0)
            await cur.execute("DELETE FROM raw_payloads WHERE id = ANY(%s)", (ids,))
            deleted["raw_payloads"] += max(cur.rowcount, 0)
            await conn.commit()

    LAST_CLEANUP_AT = started
    summary = {
        "event": "retention_cleanup",
        "started_at": started.isoformat(),
        "duration_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
        "cutoffs": {name: cutoff.isoformat() for name, cutoff in cutoffs.items()},
        "deleted": deleted,
        "raw_payload_refs_cleared": refs_cleared,
    }
    log.info(json.dumps(summary))
    return summary


async def poll_cleanup() -> None:
    while True:
        try:
            await cleanup_once()
            await asyncio.sleep(analytics.CLEANUP_INTERVAL_SECONDS)
        except Exception as err:  # noqa: BLE001 - keep the loop alive
            log.warning("retention cleanup failed: %s", err)
            await asyncio.sleep(1800)


_tasks: list[asyncio.Task] = []
_client: httpx.AsyncClient | None = None


async def start() -> None:
    global _client
    await register_sources()
    missing = missing_feed_credentials()
    if missing:
        log.warning("feed credentials not configured; those sources fail closed: %s", ", ".join(missing))
    _client = httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=30,
        follow_redirects=True,
    )
    for s in SOURCES:
        _tasks.append(asyncio.create_task(poll_source(s, _client)))
    _tasks.append(asyncio.create_task(poll_tsa_throughput(_client)))
    _tasks.append(asyncio.create_task(backfill_tsa_throughput()))
    _tasks.append(asyncio.create_task(poll_faa_events(_client)))
    _tasks.append(asyncio.create_task(poll_weather_alerts(_client)))
    _tasks.append(asyncio.create_task(poll_rollups()))
    _tasks.append(asyncio.create_task(backfill_rollups()))
    _tasks.append(asyncio.create_task(poll_cleanup()))


async def stop() -> None:
    global _client
    for t in _tasks:
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    if _client is not None:
        await _client.aclose()
        _client = None
