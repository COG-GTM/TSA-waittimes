"""Operations dashboard data aggregation."""
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from . import poller, queries

log = logging.getLogger("ops")

STARTED_AT = datetime.now(UTC)
STATUS_AMBER_SECONDS = 30 * 60
IATA_RE = re.compile(r"^[A-Z]{3}$")


def classify_status(
    last_success_at: datetime | None,
    consecutive_failures: int,
    refresh_seconds: int,
    *,
    now: datetime,
) -> str:
    if last_success_at is None or consecutive_failures > 0:
        return "red"
    age = (now - last_success_at).total_seconds()
    if age >= STATUS_AMBER_SECONDS:
        return "red"
    if age < 2 * refresh_seconds:
        return "green"
    return "amber"


def backoff_seconds(refresh_seconds: int, consecutive_failures: int) -> int:
    if consecutive_failures == 0:
        return refresh_seconds
    return min(
        refresh_seconds * 2 ** min(consecutive_failures, 4),
        poller.MAX_BACKOFF,
    )


def truncate_error(err: str | None, limit: int = 160) -> str | None:
    if err is None or len(err) <= limit:
        return err
    return err[:limit] + "…"


async def _safe(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any] | None:
    try:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()
    except Exception:
        log.warning("ops query failed", exc_info=True)
        try:
            await conn.rollback()
        except Exception:
            log.warning("ops query rollback failed", exc_info=True)
        return None


def _first(rows: list[Any] | None) -> Any:
    return rows[0][0] if rows else None


def empty_payload(now: datetime) -> dict[str, Any]:
    return {
        "generated_at": queries.iso(now),
        "sources": [],
        "system": {
            "open_checkpoints": None,
            "fresh_airports": None,
            "observations_rows": None,
            "observations_hourly_rows": None,
            "raw_payloads_rows": None,
            "last_rollup_at": None,
            "last_cleanup_at": None,
            "db_size_bytes": None,
            "uptime_seconds": int((now - STARTED_AT).total_seconds()),
            "started_at": queries.iso(STARTED_AT),
        },
        "data_sources": {
            "faa_events": {"rows": None, "latest_at": None},
            "weather_alerts": {"rows": None, "latest_at": None},
            "tsa_throughput": {"rows": None, "latest_date": None, "latest_at": None},
        },
        "status_counts": {"green": 0, "amber": 0, "red": 0},
    }


async def build_ops(conn: Any, *, now: datetime) -> dict[str, Any]:
    payload = empty_payload(now)

    source_rows = await _safe(
        conn,
        """
        SELECT s.code, s.name, s.refresh_seconds, h.last_success_at,
               h.last_attempt_at, h.last_error, h.last_error_at,
               coalesce(h.consecutive_failures, 0)
        FROM sources s
        LEFT JOIN poll_health h ON h.source_code = s.code
        ORDER BY s.code
        """,
    )
    observation_rows = await _safe(
        conn,
        """
        SELECT source_code, count(*)
        FROM observations
        WHERE fetched_at >= %s
        GROUP BY 1
        """,
        (now - timedelta(hours=1),),
    )
    observation_counts = (
        {row[0]: row[1] for row in observation_rows}
        if observation_rows is not None else None
    )

    sources: list[dict[str, Any]] = []
    status_counts = {"green": 0, "amber": 0, "red": 0}
    for code, name, refresh_seconds, last_success, last_attempt, last_error, last_error_at, failures in (
        source_rows or []
    ):
        status = classify_status(
            last_success,
            failures,
            refresh_seconds,
            now=now,
        )
        status_counts[status] += 1
        age = (
            max(0, int((now - last_success).total_seconds()))
            if last_success is not None else None
        )
        sources.append({
            "code": code,
            "iata": code if isinstance(code, str) and IATA_RE.fullmatch(code) else None,
            "name": name,
            "refresh_seconds": refresh_seconds,
            "last_success_at": queries.iso(last_success),
            "last_success_age_seconds": age,
            "last_attempt_at": queries.iso(last_attempt),
            "consecutive_failures": failures,
            "backoff_seconds": backoff_seconds(refresh_seconds, failures),
            "last_error": truncate_error(last_error),
            "last_error_at": queries.iso(last_error_at),
            "observations_last_hour": (
                observation_counts.get(code, 0)
                if observation_counts is not None else None
            ),
            "status": status,
        })
    payload["sources"] = sources
    payload["status_counts"] = status_counts

    latest_observations = await _safe(
        conn,
        """
        SELECT DISTINCT ON (checkpoint_id) is_open, fetched_at
        FROM observations
        WHERE fetched_at >= %s
        ORDER BY checkpoint_id, fetched_at DESC
        """,
        (now - timedelta(minutes=30),),
    )
    fresh_airports = await _safe(
        conn,
        """
        SELECT count(DISTINCT c.airport_iata)
        FROM observations o
        JOIN checkpoints c ON c.id = o.checkpoint_id
        WHERE o.fetched_at >= %s
        """,
        (now - timedelta(minutes=30),),
    )
    observations_count = await _safe(conn, "SELECT count(*) FROM observations")
    hourly_count = await _safe(conn, "SELECT count(*) FROM observations_hourly")
    raw_count = await _safe(conn, "SELECT count(*) FROM raw_payloads")
    last_rollup = await _safe(
        conn,
        "SELECT max(updated_at) FROM observations_hourly",
    )
    db_size = await _safe(
        conn,
        "SELECT pg_database_size(current_database())",
    )
    payload["system"].update({
        "open_checkpoints": (
            sum(1 for row in latest_observations if row[0])
            if latest_observations is not None else None
        ),
        "fresh_airports": _first(fresh_airports),
        "observations_rows": _first(observations_count),
        "observations_hourly_rows": _first(hourly_count),
        "raw_payloads_rows": _first(raw_count),
        "last_rollup_at": queries.iso(_first(last_rollup)),
        "db_size_bytes": _first(db_size),
    })

    faa_rows = await _safe(
        conn,
        "SELECT count(*), max(fetched_at) FROM faa_airport_events",
    )
    weather_rows = await _safe(
        conn,
        "SELECT count(*), max(fetched_at) FROM weather_alerts",
    )
    tsa_rows = await _safe(
        conn,
        "SELECT count(*), max(date), max(fetched_at) FROM tsa_throughput",
    )
    if faa_rows is not None:
        row = faa_rows[0] if faa_rows else (None, None)
        payload["data_sources"]["faa_events"] = {
            "rows": row[0],
            "latest_at": queries.iso(row[1]),
        }
    if weather_rows is not None:
        row = weather_rows[0] if weather_rows else (None, None)
        payload["data_sources"]["weather_alerts"] = {
            "rows": row[0],
            "latest_at": queries.iso(row[1]),
        }
    if tsa_rows is not None:
        row = tsa_rows[0] if tsa_rows else (None, None, None)
        payload["data_sources"]["tsa_throughput"] = {
            "rows": row[0],
            "latest_date": row[1].isoformat() if row[1] is not None else None,
            "latest_at": queries.iso(row[2]),
        }
    return payload
