"""Pure analytics and retention helpers."""
from __future__ import annotations

import logging
import math
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger("analytics")


def _int_env(name: str, default: int) -> int:
    """Read a positive integer environment setting, falling back on invalid values."""
    value = os.environ.get(name)
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        log.warning("invalid integer environment value for %s; using default", name)
        return default
    if parsed <= 0:
        log.warning("non-positive integer environment value for %s; using default", name)
        return default
    return parsed


ROLLUP_INTERVAL_SECONDS = _int_env("ROLLUP_INTERVAL_SECONDS", 900)
ROLLUP_LOOKBACK_HOURS = _int_env("ROLLUP_LOOKBACK_HOURS", 3)
CLEANUP_INTERVAL_SECONDS = _int_env("CLEANUP_INTERVAL_SECONDS", 21600)
RETENTION_RAW_PAYLOAD_DAYS = _int_env("RETENTION_RAW_PAYLOAD_DAYS", 14)
RETENTION_OBSERVATION_DAYS = _int_env("RETENTION_OBSERVATION_DAYS", 90)
RETENTION_FAA_EVENT_DAYS = _int_env("RETENTION_FAA_EVENT_DAYS", 180)
RETENTION_WEATHER_ALERT_DAYS = _int_env("RETENTION_WEATHER_ALERT_DAYS", 180)
CLEANUP_BATCH_LIMIT = _int_env("CLEANUP_BATCH_LIMIT", 50000)


@dataclass(frozen=True)
class ObservationRow:
    airport_iata: str
    checkpoint_id: int
    lane_type: str
    fetched_at: datetime
    wait_seconds: int | None


@dataclass(frozen=True)
class RollupRow:
    airport_iata: str
    checkpoint_id: int
    lane_type: str
    hour_bucket: datetime
    avg_wait_seconds: int | None
    max_wait_seconds: int | None
    sample_count: int


@dataclass(frozen=True)
class TypicalBucket:
    dow: int
    hour: int
    median_seconds: int | None
    p75_seconds: int | None
    sample_count: int
    observation_count: int


def hour_bucket(dt: datetime) -> datetime:
    """Return the UTC hour containing an aware datetime."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def aggregate_observations(rows: Iterable[ObservationRow]) -> list[RollupRow]:
    """Aggregate waits, rounding means with floor(mean + 0.5)."""
    grouped: dict[tuple[int, datetime], list[ObservationRow]] = {}
    for row in rows:
        key = (row.checkpoint_id, hour_bucket(row.fetched_at))
        grouped.setdefault(key, []).append(row)

    result: list[RollupRow] = []
    for (checkpoint_id, bucket), bucket_rows in grouped.items():
        valid_waits = [
            row.wait_seconds for row in bucket_rows
            if row.wait_seconds is not None and row.wait_seconds >= 0
        ]
        first = bucket_rows[0]
        sample_count = len(valid_waits)
        average = math.floor(sum(valid_waits) / sample_count + 0.5) if valid_waits else None
        maximum = max(valid_waits) if valid_waits else None
        result.append(
            RollupRow(
                airport_iata=first.airport_iata,
                checkpoint_id=checkpoint_id,
                lane_type=first.lane_type,
                hour_bucket=bucket,
                avg_wait_seconds=average,
                max_wait_seconds=maximum,
                sample_count=sample_count,
            )
        )
    return sorted(result, key=lambda row: (row.hour_bucket, row.checkpoint_id))


def retention_cutoff(now: datetime, days: int) -> datetime:
    """Return the strict retention cutoff for an aware timestamp."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("naive datetime")
    return now - timedelta(days=days)


def is_expired(ts: datetime, cutoff: datetime) -> bool:
    """Return whether a timestamp is strictly older than the cutoff."""
    return ts < cutoff


def seconds_to_minutes(seconds: int | None) -> float | None:
    """Convert seconds to minutes rounded to one decimal place."""
    return round(seconds / 60, 1) if seconds is not None else None


def _percentile(sorted_values: list[int], q: float) -> int:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    value = sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)
    return math.floor(value + 0.5)


def typical_from_hours(rows: Iterable[tuple[datetime, int, int]]) -> list[TypicalBucket]:
    """Build UTC day-of-week/hour percentiles from airport-hour values."""
    grouped: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for hour_bucket_dt, value_seconds, observation_count in rows:
        utc_hour = hour_bucket_dt.astimezone(UTC)
        key = (utc_hour.weekday(), utc_hour.hour)
        grouped.setdefault(key, []).append((value_seconds, observation_count))

    result: list[TypicalBucket] = []
    for dow in range(7):
        for hour in range(24):
            values = grouped.get((dow, hour), [])
            sorted_values = sorted(value for value, _ in values)
            result.append(
                TypicalBucket(
                    dow=dow,
                    hour=hour,
                    median_seconds=_percentile(sorted_values, 0.5) if sorted_values else None,
                    p75_seconds=_percentile(sorted_values, 0.75) if sorted_values else None,
                    sample_count=len(values),
                    observation_count=sum(count for _, count in values),
                )
            )
    return result
