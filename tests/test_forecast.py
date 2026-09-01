from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from app import forecast, main

NOW = datetime(2026, 9, 1, 17, 0, tzinfo=UTC)


def points_from(values: list[tuple[int, int]]) -> list[forecast.ObsPoint]:
    return [forecast.ObsPoint(NOW + timedelta(minutes=offset), wait) for offset, wait in values]


def profile_bucket(
    mean: float, samples: int, day_offsets: list[int], scope: str = "exact"
) -> forecast.ProfileBucket:
    dates = frozenset(NOW.date() - timedelta(days=offset) for offset in day_offsets)
    return forecast.ProfileBucket(mean, samples, dates, scope)


def steady_points(wait_seconds: int = 600) -> list[forecast.ObsPoint]:
    return [
        forecast.ObsPoint(NOW - timedelta(minutes=10 * index), wait_seconds)
        for index in range(21 * 24 * 6 + 1)
    ]


def test_steady_forecast() -> None:
    result = forecast.build_forecast(steady_points(), NOW)

    assert result.available is True
    assert result.method == "blend"
    assert result.confidence == "medium"
    assert [h.wait_seconds for h in result.horizons] == [600, 600, 600]
    assert [h.confidence for h in result.horizons] == ["high", "high", "medium"]
    assert result.basis["profile_days"] >= 3
    assert result.basis["profile_samples"] >= 18
    assert result.basis["trend_seconds_per_hour"] == 0


def test_rising_forecast_uses_trend_and_profile_blend() -> None:
    history = [
        forecast.ObsPoint(NOW - timedelta(minutes=30 * index), 3000)
        for index in range(21 * 48 + 1)
        if index * 30 > 180
    ]
    recent_run = points_from([(offset, (offset + 60) * 10) for offset in range(-60, 1, 10)])
    points = history + recent_run
    result = forecast.build_forecast(points, NOW)

    assert forecast.trend_slope(forecast.recent_points(points, NOW), NOW) > 0
    assert result.available is True
    assert result.method == "blend"
    assert result.horizons[0].wait_seconds < result.horizons[1].wait_seconds < result.horizons[2].wait_seconds

    recent = forecast.recent_points(points, NOW)
    level = forecast.recent_level(points, NOW)
    slope = forecast.trend_slope(recent, NOW)
    profile = forecast.hour_of_week_profile(points)
    profile_mean, profile_bucket = forecast.profile_lookup(
        profile, forecast.hour_of_week(NOW + timedelta(minutes=30))
    )
    assert level is not None
    assert profile_mean is not None
    assert profile_bucket is not None
    expected = round(
        (
            forecast.blend_weight(30) * (level + slope * 30)
            + (1 - forecast.blend_weight(30)) * profile_mean
        )
        / 60
    ) * 60
    assert result.horizons[0].wait_seconds == expected


def test_sparse_stale_history_is_unavailable() -> None:
    points = points_from([(-90, 600), (-70, 700), (-50, 800)])
    result = forecast.build_forecast(points, NOW)

    assert result.available is False
    assert result.reason == "stale_observations"


def test_empty_history_is_unavailable() -> None:
    result = forecast.build_forecast([], NOW)

    assert result.available is False
    assert result.reason == "insufficient_history"
    assert result.horizons == []


def test_blend_weight() -> None:
    assert forecast.blend_weight(30) == 0.8
    assert forecast.blend_weight(60) == 0.6
    assert forecast.blend_weight(120) == 0.2


def test_hour_of_week_uses_utc() -> None:
    eastern = datetime(2026, 9, 1, 13, 0, tzinfo=timezone(timedelta(hours=-4)))
    assert forecast.hour_of_week(eastern) == forecast.hour_of_week(NOW)


def test_profile_lookup_widens_and_falls_back() -> None:
    exact, exact_bucket = forecast.profile_lookup({10: profile_bucket(900.0, 3, [0, 7])}, 10)
    assert exact == 900.0
    assert exact_bucket == profile_bucket(900.0, 3, [0, 7])
    neighbor, neighbor_bucket = forecast.profile_lookup(
        {9: profile_bucket(600.0, 2, [0, 7]), 11: profile_bucket(1200.0, 2, [0, 7])},
        10,
    )
    assert neighbor == 900.0
    assert neighbor_bucket == profile_bucket(900.0, 4, [0, 7], "nearby")
    profile = {20: profile_bucket(600.0, 6, [0]), 40: profile_bucket(1200.0, 6, [7])}
    global_mean, global_bucket = forecast.profile_lookup(profile, 80)
    assert global_mean == 900.0
    assert global_bucket == profile_bucket(900.0, 12, [0, 7], "global")
    assert forecast.profile_lookup({20: profile_bucket(600.0, 2, [0])}, 80) == (None, None)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {"recent_count": 6, "latest_age_minutes": 10, "bucket_days": 3, "bucket_scope": "exact", "horizon_minutes": 30},
            "high",
        ),
        (
            {"recent_count": 6, "latest_age_minutes": 10, "bucket_days": 1, "bucket_scope": "exact", "horizon_minutes": 120},
            "medium",
        ),
        (
            {"recent_count": 2, "latest_age_minutes": 10, "bucket_days": 1, "bucket_scope": "exact", "horizon_minutes": 30},
            "low",
        ),
    ],
)
def test_confidence_label(kwargs: dict[str, object], expected: str) -> None:
    assert forecast.confidence_label(**kwargs) == expected


def test_slope_is_clamped() -> None:
    recent = points_from([(-60, 0), (-30, 1200), (0, 2400)])
    assert forecast.trend_slope(recent, NOW) == forecast.MAX_SLOPE_SECONDS_PER_MINUTE


def test_profile_counts_distinct_utc_days() -> None:
    same_bucket_different_days = [
        forecast.ObsPoint(NOW - timedelta(days=7 * index), 600)
        for index in range(3)
    ]
    profile = forecast.hour_of_week_profile(same_bucket_different_days)
    bucket = profile[forecast.hour_of_week(NOW)]
    assert bucket.samples == 3
    assert bucket.days == 3

    same_day_many_polls = [
        forecast.ObsPoint(NOW + timedelta(minutes=10 * index), 600)
        for index in range(6)
    ]
    same_day_profile = forecast.hour_of_week_profile(same_day_many_polls)
    same_day_bucket = same_day_profile[forecast.hour_of_week(NOW)]
    assert same_day_bucket.samples == 6
    assert same_day_bucket.days == 1


def test_many_samples_on_one_day_are_not_strong_profile_support() -> None:
    points = [
        forecast.ObsPoint(NOW + timedelta(minutes=5 * index), 600)
        for index in range(12)
    ]
    profile = forecast.hour_of_week_profile(points)
    mean, bucket = forecast.profile_lookup(profile, forecast.hour_of_week(NOW))
    assert mean == 600
    assert bucket is not None
    assert bucket.samples == 12
    assert bucket.days == 1
    assert forecast.confidence_label(
        recent_count=0,
        latest_age_minutes=None,
        bucket_days=bucket.days,
        bucket_scope=bucket.scope,
        horizon_minutes=30,
    ) == "low"


def test_global_profile_support_is_not_strong() -> None:
    profile = {
        20: profile_bucket(600.0, 4, [0]),
        40: profile_bucket(900.0, 4, [7]),
        60: profile_bucket(1200.0, 4, [14]),
    }
    mean, bucket = forecast.profile_lookup(profile, 100)

    assert mean == 900.0
    assert bucket is not None
    assert bucket.scope == "global"
    assert bucket.days == 3
    assert forecast.confidence_label(
        recent_count=0,
        latest_age_minutes=None,
        bucket_days=bucket.days,
        bucket_scope=bucket.scope,
        horizon_minutes=30,
    ) == "low"


def test_stale_recent_observations_use_profile_only() -> None:
    history = [
        forecast.ObsPoint(NOW - timedelta(minutes=210 + 10 * index), 3000)
        for index in range(21 * 24 * 6)
    ]
    stale_recent = points_from([(-105, 600), (-75, 1200), (-45, 1800)])
    result = forecast.build_forecast(history + stale_recent, NOW)

    assert result.available is True
    assert result.method == "profile_only"
    assert result.basis["latest_observation_age_minutes"] == 45
    assert result.basis["trend_seconds_per_hour"] is None


def test_stale_thin_history_is_unavailable() -> None:
    result = forecast.build_forecast(points_from([(-90, 600), (-70, 700), (-50, 800)]), NOW)

    assert result.available is False
    assert result.reason == "stale_observations"


@pytest.mark.parametrize("method", ["blend", "trend_only", "profile_only", "none"])
def test_payload_method_note_matches_method(method: str) -> None:
    result = forecast.Forecast(
        available=method != "none",
        reason=None if method != "none" else "insufficient_history",
        method=method,
        confidence="low",
        horizons=[],
        basis={},
    )
    payload = forecast._forecast_payload({"iata": "JFK", "name": "JFK"}, result, NOW)

    assert payload["method_note"] == forecast.METHOD_NOTES[method]


class FakeCursor:
    def __init__(self, airport_row: tuple[str, str] | None, observation_rows: list[tuple[datetime, int]]):
        self.airport_row = airport_row
        self.observation_rows = observation_rows
        self.current_query = ""
        self.airport_queries = 0
        self.observation_queries = 0

    async def execute(self, query: str, _params: tuple[object, ...] = ()) -> None:
        self.current_query = query
        if "FROM airports WHERE iata" in query:
            self.airport_queries += 1
        elif "WITH per_minute AS" in query:
            self.observation_queries += 1

    async def fetchone(self):
        if "FROM airports WHERE iata" in self.current_query:
            return self.airport_row
        raise AssertionError(f"unexpected fetchone query: {self.current_query}")

    async def fetchall(self):
        if "WITH per_minute AS" in self.current_query:
            return self.observation_rows
        raise AssertionError(f"unexpected fetchall query: {self.current_query}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self.cursor_obj = cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def cursor(self):
        return self.cursor_obj


class FakePool:
    def __init__(self, cursor: FakeCursor):
        self.cursor = cursor

    def connection(self):
        return FakeConnection(self.cursor)


def rich_rows() -> list[tuple[datetime, int]]:
    now = datetime.now(UTC)
    return [
        (now - timedelta(minutes=30 * index), 600)
        for index in range(21 * 48 + 1)
    ]


@pytest.fixture(autouse=True)
def clear_forecast_cache() -> None:
    forecast.clear_cache()


@pytest.mark.asyncio
async def test_forecast_endpoint_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = FakeCursor(("JFK", "John F. Kennedy International Airport"), rich_rows())
    monkeypatch.setattr(main.db, "pool", FakePool(cursor))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/api/airport/jfk/forecast")
        second = await client.get("/api/airport/JFK/forecast")

    assert response.status_code == 200
    assert response.json()["available"] is True
    assert len(response.json()["horizons"]) == 3
    assert all(h["confidence"] in {"low", "medium", "high"} for h in response.json()["horizons"])
    assert second.json() == response.json()
    assert cursor.observation_queries == 1

    forecast.clear_cache()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/api/airport/JFK/forecast")
    assert response.status_code == 200
    assert cursor.observation_queries == 2


@pytest.mark.asyncio
async def test_forecast_endpoint_rejects_unknown_and_malformed_iata(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = FakeCursor(None, [])
    monkeypatch.setattr(main.db, "pool", FakePool(cursor))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        unknown = await client.get("/api/airport/ZZZ/forecast")
        malformed = await client.get("/api/airport/TOOLONG/forecast")
        traversal = await client.get("/api/airport/../etc/forecast")

    assert unknown.status_code == 404
    assert malformed.status_code == 404
    assert traversal.status_code == 404
    assert cursor.airport_queries == 1


@pytest.mark.asyncio
async def test_forecast_endpoint_stale_history_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = FakeCursor(
        ("JFK", "John F. Kennedy International Airport"),
        [(datetime.now(UTC) - timedelta(minutes=60), 600)],
    )
    monkeypatch.setattr(main.db, "pool", FakePool(cursor))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/api/airport/JFK/forecast")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["reason"] == "stale_observations"


@pytest.mark.asyncio
async def test_forecast_endpoint_fresh_thin_history_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    cursor = FakeCursor(
        ("JFK", "John F. Kennedy International Airport"),
        [(now - timedelta(minutes=3), 600)],
    )
    monkeypatch.setattr(main.db, "pool", FakePool(cursor))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/api/airport/JFK/forecast")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["reason"] == "insufficient_history"
