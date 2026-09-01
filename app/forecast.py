"""Short-horizon standard-lane wait forecasts.

The forecast combines the recent level and direction of an airport's
standard-lane observations with an hour-of-week profile built from distinct
UTC calendar days over the recent history. Recent observations matter most
for the nearest horizon, but are excluded from the trend when they are stale.
Observations after the forecast's reference time are discarded before any
forecast input is calculated.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

HORIZON_MINUTES: tuple[int, ...] = (30, 60, 120)
RECENT_WINDOW_MINUTES = 180
LEVEL_WINDOW_MINUTES = 30
HISTORY_DAYS = 21
CACHE_TTL_SECONDS = 300
FRESH_MINUTES = 20
MIN_RECENT_POINTS = 2
MIN_PROFILE_SAMPLES = 12
STRONG_RECENT_POINTS = 6
STRONG_BUCKET_DAYS = 3
MAX_WAIT_SECONDS = 7200
MAX_SLOPE_SECONDS_PER_MINUTE = 20.0
TREND_STALE_MINUTES = 30


@dataclass(frozen=True)
class ObsPoint:
    """A UTC observation used as input to the forecast."""

    at: datetime
    wait_seconds: int


@dataclass(frozen=True)
class HorizonForecast:
    """A forecast for one future horizon."""

    horizon_minutes: int
    valid_at: datetime
    wait_seconds: int
    wait_minutes: int
    confidence: str


@dataclass(frozen=True)
class Forecast:
    """The complete forecast and the inputs supporting it."""

    available: bool
    reason: str | None
    method: str
    confidence: str
    horizons: list[HorizonForecast]
    basis: dict[str, object]


class ForecastCursor(Protocol):
    """The async cursor operations needed by the forecast query layer."""

    async def execute(self, query: str, params: tuple[str, datetime]) -> object:
        ...

    async def fetchone(self) -> tuple[Any, ...] | None:
        ...

    async def fetchall(self) -> list[tuple[Any, ...]]:
        ...


@dataclass(frozen=True)
class ProfileBucket:
    """Summary of observations in one UTC hour-of-week bucket."""

    mean: float
    samples: int
    dates: frozenset[date]
    scope: str

    @property
    def days(self) -> int:
        return len(self.dates)


FORECAST_SQL = """
WITH per_minute AS (
    SELECT date_trunc('minute', o.fetched_at) AS minute, max(o.wait_seconds) AS wait_seconds
    FROM observations o
    JOIN checkpoints c ON c.id = o.checkpoint_id
    WHERE c.airport_iata = %s
      AND c.lane_type = 'standard'
      AND o.wait_seconds IS NOT NULL
      AND o.is_open
      AND o.fetched_at > %s
    GROUP BY 1
)
SELECT minute, wait_seconds FROM per_minute ORDER BY minute
"""

LANE_STATE_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (o.checkpoint_id) o.checkpoint_id, o.is_open, o.fetched_at
    FROM observations o
    JOIN checkpoints c ON c.id = o.checkpoint_id
    WHERE c.airport_iata = %s
      AND c.lane_type = 'standard'
      AND o.fetched_at > %s
    ORDER BY o.checkpoint_id, o.fetched_at DESC
)
SELECT max(fetched_at) AS latest_fetched_at, bool_or(is_open) AS any_open
FROM latest
"""

METHOD_NOTES = {
    "blend": (
        f"Blend of the last {RECENT_WINDOW_MINUTES // 60} hours of standard-lane observations "
        f"with this airport's hour-of-week average over the past {HISTORY_DAYS} days "
        "(UTC hour buckets)."
    ),
    "trend_only": (
        f"Extrapolated from the last {RECENT_WINDOW_MINUTES // 60} hours of standard-lane "
        "observations; too little history for an hour-of-week average."
    ),
    "profile_only": (
        f"This airport's hour-of-week average over the past {HISTORY_DAYS} days "
        "(UTC hour buckets); recent observations were missing or stale, or the standard "
        "lanes are closed."
    ),
    "none": "Not enough recent or historical standard-lane observations to forecast.",
}

_cache: dict[str, tuple[float, dict[str, object]]] = {}


def recent_points(points: Sequence[ObsPoint], now: datetime) -> list[ObsPoint]:
    """Return observations in the recent window, ordered chronologically."""

    start = now - timedelta(minutes=RECENT_WINDOW_MINUTES)
    return sorted((point for point in points if start <= point.at <= now), key=lambda point: point.at)


def recent_level(points: Sequence[ObsPoint], now: datetime) -> float | None:
    """Return the recent mean level, falling back to the latest recent point."""

    recent = recent_points(points, now)
    if not recent:
        return None
    level_start = now - timedelta(minutes=LEVEL_WINDOW_MINUTES)
    level_points = [point for point in recent if point.at >= level_start]
    if level_points:
        return sum(point.wait_seconds for point in level_points) / len(level_points)
    return float(recent[-1].wait_seconds)


def trend_slope(recent: Sequence[ObsPoint], now: datetime) -> float:
    """Calculate the bounded least-squares trend in seconds per clock-minute."""

    if len(recent) < 3:
        return 0.0
    x_values = [(point.at - now).total_seconds() / 60.0 for point in recent]
    if max(x_values) - min(x_values) < 30:
        return 0.0
    x_mean = sum(x_values) / len(x_values)
    y_mean = sum(point.wait_seconds for point in recent) / len(recent)
    x_variance = sum((x - x_mean) ** 2 for x in x_values)
    if x_variance == 0:
        return 0.0
    covariance = sum(
        (x - x_mean) * (point.wait_seconds - y_mean)
        for x, point in zip(x_values, recent)
    )
    slope = covariance / x_variance
    return max(-MAX_SLOPE_SECONDS_PER_MINUTE, min(MAX_SLOPE_SECONDS_PER_MINUTE, slope))


def hour_of_week(dt: datetime) -> int:
    """Return the UTC hour bucket, from Monday 00:00 through Sunday 23:00."""

    utc_dt = dt.astimezone(UTC)
    return utc_dt.weekday() * 24 + utc_dt.hour


def hour_of_week_profile(points: Sequence[ObsPoint]) -> dict[int, ProfileBucket]:
    """Aggregate all observations by UTC hour-of-week bucket."""

    buckets: dict[int, list[int]] = {}
    dates: dict[int, set[date]] = {}
    for point in points:
        bucket = hour_of_week(point.at)
        buckets.setdefault(bucket, []).append(point.wait_seconds)
        dates.setdefault(bucket, set()).add(point.at.astimezone(UTC).date())
    return {
        bucket: ProfileBucket(
            mean=sum(values) / len(values),
            samples=len(values),
            dates=frozenset(dates[bucket]),
            scope="exact",
        )
        for bucket, values in buckets.items()
    }


def _pooled_bucket(
    profile: dict[int, ProfileBucket], buckets: Sequence[int], scope: str
) -> ProfileBucket | None:
    values = [profile[item] for item in buckets if item in profile]
    samples = sum(value.samples for value in values)
    if samples <= 0:
        return None
    pooled_dates = frozenset(date_value for value in values for date_value in value.dates)
    return ProfileBucket(
        mean=sum(value.mean * value.samples for value in values) / samples,
        samples=samples,
        dates=pooled_dates,
        scope=scope,
    )


def profile_lookup(
    profile: dict[int, ProfileBucket], bucket: int
) -> tuple[float | None, ProfileBucket | None]:
    """Look up a bucket, widening to nearby buckets or the whole profile."""

    exact = profile.get(bucket)
    if exact is not None and exact.days >= 2:
        return exact.mean, ProfileBucket(exact.mean, exact.samples, exact.dates, "exact")

    nearby_buckets = [(bucket + offset) % 168 for offset in (-1, 0, 1)]
    nearby = _pooled_bucket(profile, nearby_buckets, "nearby")
    if nearby is not None and nearby.days >= 2:
        return nearby.mean, nearby

    total_samples = sum(value.samples for value in profile.values())
    if total_samples >= MIN_PROFILE_SAMPLES:
        all_buckets = list(profile)
        global_bucket = _pooled_bucket(profile, all_buckets, "global")
        if global_bucket is not None:
            return global_bucket.mean, global_bucket
    return None, None


def blend_weight(horizon_minutes: int) -> float:
    """Return the weight assigned to the recent trend at a horizon."""

    return max(0.2, 1.0 - horizon_minutes / 150.0)


def confidence_label(
    *,
    recent_count: int,
    latest_age_minutes: float | None,
    bucket_days: int,
    bucket_scope: str,
    horizon_minutes: int,
) -> str:
    """Classify confidence from freshness, recent data, and profile support."""

    fresh = latest_age_minutes is not None and latest_age_minutes <= FRESH_MINUTES
    strong_recent = fresh and recent_count >= STRONG_RECENT_POINTS
    strong_profile = bucket_days >= STRONG_BUCKET_DAYS and bucket_scope != "global"
    if strong_recent and strong_profile and horizon_minutes <= 60:
        return "high"
    if strong_recent or strong_profile:
        return "medium"
    return "low"


def _clamp_wait(wait_seconds: float) -> float:
    return max(0.0, min(MAX_WAIT_SECONDS, wait_seconds))


def _overall_confidence(labels: Sequence[str]) -> str:
    rank = {"low": 0, "medium": 1, "high": 2}
    return min(labels, key=lambda label: rank[label], default="low")


def build_forecast(
    points: Sequence[ObsPoint], now: datetime, *, lanes_open: bool | None = None
) -> Forecast:
    """Build a forecast from points at or before the reference time."""

    bounded_points = [point for point in points if point.at <= now]
    recent = recent_points(bounded_points, now)
    level = recent_level(bounded_points, now)
    slope = trend_slope(recent, now)
    profile = hour_of_week_profile(bounded_points)
    total_history = len(bounded_points)
    latest = max(bounded_points, key=lambda point: point.at, default=None)
    latest_age = (
        (now - latest.at).total_seconds() / 60.0
        if latest is not None
        else None
    )
    have_trend = (
        level is not None
        and len(recent) >= MIN_RECENT_POINTS
        and latest_age is not None
        and latest_age <= TREND_STALE_MINUTES
        and lanes_open is not False
    )
    have_profile = total_history >= MIN_PROFILE_SAMPLES
    basis: dict[str, object] = {
        "recent_points": len(recent),
        "recent_level_seconds": round(level) if level is not None else None,
        "trend_seconds_per_hour": round(slope * 60) if have_trend else None,
        "history_points": total_history,
        "history_days": HISTORY_DAYS,
        "latest_observation_at": latest.at.astimezone(UTC).isoformat() if latest else None,
        "latest_observation_age_minutes": round(latest_age) if latest_age is not None else None,
        "window_minutes": RECENT_WINDOW_MINUTES,
        "profile_days": 0,
        "profile_samples": 0,
        "profile_scope": None,
        "standard_lanes_open": lanes_open,
    }

    if not have_trend and not have_profile:
        reason = (
            "checkpoints_closed"
            if lanes_open is False
            else "stale_observations"
            if latest_age is not None and latest_age > TREND_STALE_MINUTES
            else "insufficient_history"
        )
        return Forecast(
            available=False,
            reason=reason,
            method="none",
            confidence="low",
            horizons=[],
            basis=basis,
        )

    method = "blend" if have_trend and have_profile else "trend_only" if have_trend else "profile_only"
    horizons: list[HorizonForecast] = []
    labels: list[str] = []
    profile_support: ProfileBucket | None = None
    scope_rank = {"exact": 0, "nearby": 1, "global": 2}
    for horizon in HORIZON_MINUTES:
        valid_at = now + timedelta(minutes=horizon)
        trend_pred = _clamp_wait(level + slope * horizon) if have_trend and level is not None else None
        profile_pred, profile_bucket = profile_lookup(profile, hour_of_week(valid_at))
        if not have_profile or profile_pred is None:
            profile_pred = None
            profile_bucket = None
        if profile_bucket is not None and (
            profile_support is None
            or scope_rank[profile_bucket.scope] > scope_rank[profile_support.scope]
            or (
                scope_rank[profile_bucket.scope] == scope_rank[profile_support.scope]
                and profile_bucket.days < profile_support.days
            )
        ):
            profile_support = profile_bucket

        if trend_pred is not None and profile_pred is not None:
            prediction = blend_weight(horizon) * trend_pred + (1 - blend_weight(horizon)) * profile_pred
        elif trend_pred is not None:
            prediction = trend_pred
        elif profile_pred is not None:
            prediction = profile_pred
        else:
            continue

        wait_seconds = round(_clamp_wait(prediction) / 60.0) * 60
        label = confidence_label(
            recent_count=len(recent),
            latest_age_minutes=latest_age,
            bucket_days=profile_bucket.days if profile_bucket is not None else 0,
            bucket_scope=profile_bucket.scope if profile_bucket is not None else "",
            horizon_minutes=horizon,
        )
        labels.append(label)
        horizons.append(
            HorizonForecast(
                horizon_minutes=horizon,
                valid_at=valid_at.astimezone(UTC),
                wait_seconds=wait_seconds,
                wait_minutes=wait_seconds // 60,
                confidence=label,
            )
        )

    if profile_support is not None:
        basis["profile_days"] = profile_support.days
        basis["profile_samples"] = profile_support.samples
        basis["profile_scope"] = profile_support.scope
    return Forecast(
        available=True,
        reason=None,
        method=method,
        confidence=_overall_confidence(labels),
        horizons=horizons,
        basis=basis,
    )


async def load_points(cur: ForecastCursor, iata: str, now: datetime) -> list[ObsPoint]:
    """Load recent standard-lane observations for an airport."""

    cutoff = now - timedelta(days=HISTORY_DAYS)
    await cur.execute(FORECAST_SQL, (iata, cutoff))
    points = []
    for row in await cur.fetchall():
        at = row[0].astimezone(UTC)
        points.append(ObsPoint(at=at, wait_seconds=int(row[1])))
    return points


async def load_lane_state(cur: ForecastCursor, iata: str, now: datetime) -> bool | None:
    """Load whether any standard checkpoint is currently open."""

    cutoff = now - timedelta(days=HISTORY_DAYS)
    await cur.execute(LANE_STATE_SQL, (iata, cutoff))
    row = await cur.fetchone()
    if row is None or row[1] is None:
        return None
    return bool(row[1])


def _forecast_payload(airport: dict[str, str], forecast: Forecast, now: datetime) -> dict[str, object]:
    """Serialize a forecast into the public API payload."""

    return {
        "airport": airport,
        "generated_at": now.astimezone(UTC).isoformat(),
        "lane_type": "standard",
        "available": forecast.available,
        "reason": forecast.reason,
        "method": forecast.method,
        "confidence": forecast.confidence,
        "horizons": [
            {
                "horizon_minutes": horizon.horizon_minutes,
                "valid_at": horizon.valid_at.astimezone(UTC).isoformat(),
                "wait_seconds": horizon.wait_seconds,
                "wait_minutes": horizon.wait_minutes,
                "confidence": horizon.confidence,
            }
            for horizon in forecast.horizons
        ],
        "basis": forecast.basis,
        "method_note": METHOD_NOTES[forecast.method],
    }


async def get_forecast(
    cur: ForecastCursor,
    iata: str,
    name: str,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return a cached or newly computed airport forecast payload."""

    explicit_now = now is not None
    current = (now if now is not None else datetime.now(UTC)).astimezone(UTC)
    if not explicit_now:
        cached = _cache.get(iata)
        if cached is not None:
            cached_at, payload = cached
            if time.monotonic() - cached_at < CACHE_TTL_SECONDS:
                return payload

    points = await load_points(cur, iata, current)
    lanes_open = await load_lane_state(cur, iata, current)
    result = _forecast_payload(
        {"iata": iata, "name": name},
        build_forecast(points, current, lanes_open=lanes_open),
        current,
    )
    if not explicit_now:
        _cache[iata] = (time.monotonic(), result)
    return result


def clear_cache() -> None:
    """Clear the in-process forecast cache."""

    _cache.clear()
