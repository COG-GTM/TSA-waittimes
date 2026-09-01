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
    hours = frozenset(
        (NOW - timedelta(days=offset)).replace(minute=0, second=0, microsecond=0)
        for offset in day_offsets
    )
    return forecast.ProfileBucket(mean, samples, hours, scope)


def profile_support(
    mean: float, samples: int, occurrences: int, scope: str
) -> forecast.ProfileSupport:
    return forecast.ProfileSupport(mean, samples, occurrences, scope)


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
    assert result.basis["profile_occurrences"] >= 3
    assert result.basis["profile_samples"] >= 18
    assert result.basis["trend_seconds_per_hour"] == 0


def test_closed_lanes_disable_trend_on_rich_history() -> None:
    result = forecast.build_forecast(steady_points(), NOW, lanes_open=False)

    assert result.available is True
    assert result.method == "profile_only"
    assert result.basis["standard_lanes_open"] is False
    assert result.basis["trend_seconds_per_hour"] is None


def test_closed_lanes_make_thin_history_unavailable() -> None:
    result = forecast.build_forecast(
        points_from([(-5, 600), (-3, 600)]),
        NOW,
        lanes_open=False,
    )

    assert result.available is False
    assert result.reason == "checkpoints_closed"
    assert result.basis["standard_lanes_open"] is False


def test_unknown_and_open_lane_state_have_same_forecast() -> None:
    unknown = forecast.build_forecast(steady_points(), NOW)
    open_lanes = forecast.build_forecast(steady_points(), NOW, lanes_open=True)

    assert unknown.available == open_lanes.available
    assert unknown.reason == open_lanes.reason
    assert unknown.method == open_lanes.method
    assert unknown.confidence == open_lanes.confidence
    assert unknown.horizons == open_lanes.horizons
    assert {**unknown.basis, "standard_lanes_open": True} == open_lanes.basis


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


def test_forecast_helpers_exclude_future_points() -> None:
    past = forecast.ObsPoint(NOW - timedelta(minutes=5), 600)
    future = forecast.ObsPoint(NOW + timedelta(minutes=5), 3600)
    points = [past, future]

    assert forecast.recent_points(points, NOW) == [past]
    bounded_points = [point for point in points if point.at <= NOW]
    profile = forecast.hour_of_week_profile(bounded_points)
    assert profile[forecast.hour_of_week(past.at)].samples == 1


def test_profile_lookup_widens_and_falls_back() -> None:
    exact, exact_bucket = forecast.profile_lookup({10: profile_bucket(900.0, 3, [0, 7])}, 10)
    assert exact == 900.0
    assert exact_bucket == profile_support(900.0, 3, 2, "exact")
    neighbor, neighbor_bucket = forecast.profile_lookup(
        {9: profile_bucket(600.0, 2, [0, 7]), 11: profile_bucket(1200.0, 2, [0, 7])},
        10,
    )
    assert neighbor == 900.0
    assert neighbor_bucket == profile_support(900.0, 4, 2, "nearby")
    profile = {20: profile_bucket(600.0, 6, [0]), 40: profile_bucket(1200.0, 6, [7])}
    global_mean, global_bucket = forecast.profile_lookup(profile, 80)
    assert global_mean == 900.0
    assert global_bucket == profile_support(900.0, 12, 2, "global")
    assert forecast.profile_lookup({20: profile_bucket(600.0, 2, [0])}, 80) == (None, None)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {"recent_count": 6, "latest_age_minutes": 10, "bucket_occurrences": 3, "bucket_scope": "exact", "horizon_minutes": 30},
            "high",
        ),
        (
            {"recent_count": 6, "latest_age_minutes": 10, "bucket_occurrences": 1, "bucket_scope": "exact", "horizon_minutes": 120},
            "medium",
        ),
        (
            {"recent_count": 2, "latest_age_minutes": 10, "bucket_occurrences": 1, "bucket_scope": "exact", "horizon_minutes": 30},
            "low",
        ),
    ],
)
def test_confidence_label(kwargs: dict[str, object], expected: str) -> None:
    assert forecast.confidence_label(**kwargs) == expected


def test_slope_is_clamped() -> None:
    recent = points_from([(-60, 0), (-30, 1200), (0, 2400)])
    assert forecast.trend_slope(recent, NOW) == forecast.MAX_SLOPE_SECONDS_PER_MINUTE


def test_profile_counts_distinct_occurrences() -> None:
    same_bucket_different_days = [
        forecast.ObsPoint(NOW - timedelta(days=7 * index), 600)
        for index in range(3)
    ]
    profile = forecast.hour_of_week_profile(same_bucket_different_days)
    bucket = profile[forecast.hour_of_week(NOW)]
    assert bucket.samples == 3
    assert forecast.bucket_occurrences(bucket.hours, forecast.hour_of_week(NOW)) == 3

    same_day_many_polls = [
        forecast.ObsPoint(NOW + timedelta(minutes=10 * index), 600)
        for index in range(6)
    ]
    same_day_profile = forecast.hour_of_week_profile(same_day_many_polls)
    same_day_bucket = same_day_profile[forecast.hour_of_week(NOW)]
    assert same_day_bucket.samples == 6
    assert forecast.bucket_occurrences(same_day_bucket.hours, forecast.hour_of_week(NOW)) == 1


def test_nearby_profile_collapses_same_weekend_boundary() -> None:
    sunday = datetime(2026, 8, 30, 23, 0, tzinfo=UTC)
    points = [
        forecast.ObsPoint(sunday, 600),
        forecast.ObsPoint(sunday + timedelta(hours=1), 600),
    ]
    profile = forecast.hour_of_week_profile(points)
    nearby = forecast._pooled_bucket(profile, [167, 0, 1], "nearby")

    assert nearby is not None
    assert forecast.bucket_occurrences(nearby.hours, 0) == 1
    assert forecast.profile_lookup(profile, 0) == (None, None)


def test_nearby_profile_counts_distinct_weekend_occurrences() -> None:
    weekends = [
        datetime(2026, 8, 23, 23, 0, tzinfo=UTC),
        datetime(2026, 8, 30, 23, 0, tzinfo=UTC),
    ]
    points = [
        forecast.ObsPoint(sunday + timedelta(hours=offset), 600)
        for sunday in weekends
        for offset in (0, 1)
    ]
    profile = forecast.hour_of_week_profile(points)
    nearby = forecast._pooled_bucket(profile, [167, 0, 1], "nearby")
    mean, support = forecast.profile_lookup(profile, 0)

    assert nearby is not None
    assert forecast.bucket_occurrences(nearby.hours, 0) == 2
    assert mean == 600
    assert support is not None
    assert support.occurrences == 2


def test_exact_profile_counts_three_weekly_occurrences() -> None:
    points = [
        forecast.ObsPoint(NOW - timedelta(days=7 * index), 600)
        for index in range(3)
    ]
    profile = forecast.hour_of_week_profile(points)
    mean, support = forecast.profile_lookup(profile, forecast.hour_of_week(NOW))

    assert mean == 600
    assert support is not None
    assert support.scope == "exact"
    assert support.occurrences == 3


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
    assert bucket.occurrences == 1
    assert forecast.confidence_label(
        recent_count=0,
        latest_age_minutes=None,
        bucket_occurrences=bucket.occurrences,
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
    assert bucket.occurrences == 3
    assert forecast.confidence_label(
        recent_count=0,
        latest_age_minutes=None,
        bucket_occurrences=bucket.occurrences,
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


def test_future_observation_cannot_enable_stale_trend() -> None:
    history = [
        forecast.ObsPoint(NOW - timedelta(minutes=210 + 10 * index), 3000)
        for index in range(21 * 24 * 6)
    ]
    stale_recent = points_from([(-105, 600), (-75, 1200), (-45, 1800)])
    future = forecast.ObsPoint(NOW + timedelta(minutes=5), 3600)
    result = forecast.build_forecast(history + stale_recent + [future], NOW)

    assert result.available is True
    assert result.method == "profile_only"
    assert result.basis["latest_observation_age_minutes"] == 45
    assert result.basis["history_points"] == len(history) + len(stale_recent)


def test_basis_uses_one_weakest_profile_bucket() -> None:
    exact_points = [
        forecast.ObsPoint(NOW - timedelta(days=7 * index), 600)
        for index in range(3)
    ]
    global_bucket_at = datetime(2026, 8, 28, 4, tzinfo=UTC)
    global_points = [
        forecast.ObsPoint(global_bucket_at - timedelta(days=7 * index), 1200)
        for index in range(3)
        for _ in range(3)
    ]
    points = exact_points + global_points
    profile = forecast.hour_of_week_profile(points)
    exact_value, exact_bucket = forecast.profile_lookup(profile, forecast.hour_of_week(NOW + timedelta(minutes=30)))
    nearby_value, nearby_bucket = forecast.profile_lookup(profile, forecast.hour_of_week(NOW + timedelta(minutes=60)))
    global_value, global_bucket = forecast.profile_lookup(profile, forecast.hour_of_week(NOW + timedelta(minutes=120)))
    result = forecast.build_forecast(points, NOW)

    assert exact_value is not None
    assert exact_bucket is not None
    assert exact_bucket.scope == "exact"
    assert nearby_value is not None
    assert nearby_bucket is not None
    assert nearby_bucket.scope == "nearby"
    assert global_value is not None
    assert global_bucket is not None
    assert global_bucket.scope == "global"
    assert result.basis["profile_occurrences"] == global_bucket.occurrences
    assert result.basis["profile_samples"] == global_bucket.samples
    assert result.basis["profile_scope"] == global_bucket.scope


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
    def __init__(
        self,
        airport_row: tuple[str, str] | None,
        observation_rows: list[tuple[datetime, int]],
        lanes_open: bool | None = True,
    ):
        self.airport_row = airport_row
        self.observation_rows = observation_rows
        self.lanes_open = lanes_open
        self.current_query = ""
        self.airport_queries = 0
        self.observation_queries = 0
        self.lane_state_queries = 0

    async def execute(self, query: str, _params: tuple[object, ...] = ()) -> None:
        self.current_query = query
        if "FROM airports WHERE iata" in query:
            self.airport_queries += 1
        elif "WITH per_minute AS" in query:
            self.observation_queries += 1

    async def fetchone(self):
        if "FROM airports WHERE iata" in self.current_query:
            return self.airport_row
        if "WITH latest AS" in self.current_query:
            self.lane_state_queries += 1
            return (None, self.lanes_open)
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
    return rich_rows_at(datetime.now(UTC))


def rich_rows_at(now: datetime) -> list[tuple[datetime, int]]:
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
async def test_forecast_endpoint_closed_lanes_uses_profile_only(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = FakeCursor(
        ("JFK", "John F. Kennedy International Airport"),
        rich_rows(),
        lanes_open=False,
    )
    monkeypatch.setattr(main.db, "pool", FakePool(cursor))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/api/airport/JFK/forecast")

    payload = response.json()
    assert response.status_code == 200
    assert payload["available"] is True
    assert payload["method"] == "profile_only"
    assert payload["basis"]["standard_lanes_open"] is False
    assert payload["basis"]["trend_seconds_per_hour"] is None
    assert cursor.lane_state_queries == 1


@pytest.mark.asyncio
async def test_explicit_now_bypasses_forecast_cache() -> None:
    cursor = FakeCursor(
        ("JFK", "John F. Kennedy International Airport"),
        rich_rows_at(NOW),
    )
    first = await forecast.get_forecast(cursor, "JFK", "John F. Kennedy International Airport", now=NOW)
    second_now = NOW + timedelta(minutes=5)
    second = await forecast.get_forecast(
        cursor,
        "JFK",
        "John F. Kennedy International Airport",
        now=second_now,
    )

    assert first["generated_at"] != second["generated_at"]
    assert first["horizons"][0]["valid_at"] != second["horizons"][0]["valid_at"]
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
