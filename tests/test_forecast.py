from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import forecast, main

NOW = datetime(2026, 9, 1, 17, 0, tzinfo=UTC)


def points_from(values: list[tuple[int, int]]) -> list[forecast.ObsPoint]:
    return [forecast.ObsPoint(NOW + timedelta(minutes=offset), wait) for offset, wait in values]


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
    profile_mean, _ = forecast.profile_lookup(profile, forecast.hour_of_week(NOW + timedelta(minutes=30)))
    assert level is not None
    assert profile_mean is not None
    expected = round(
        (
            forecast.blend_weight(30) * (level + slope * 30)
            + (1 - forecast.blend_weight(30)) * profile_mean
        )
        / 60
    ) * 60
    assert result.horizons[0].wait_seconds == expected


def test_sparse_recent_history_is_trend_only() -> None:
    points = points_from([(-90, 600), (-70, 700), (-50, 800)])
    result = forecast.build_forecast(points, NOW)

    assert result.available is True
    assert result.method == "trend_only"
    assert all(h.confidence == "low" for h in result.horizons)


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
    from datetime import timezone

    eastern = datetime(2026, 9, 1, 13, 0, tzinfo=timezone(timedelta(hours=-4)))
    assert forecast.hour_of_week(eastern) == forecast.hour_of_week(NOW)


def test_profile_lookup_widens_and_falls_back() -> None:
    assert forecast.profile_lookup({10: (900.0, 3)}, 10) == (900.0, 3)
    assert forecast.profile_lookup({9: (600.0, 2), 11: (1200.0, 2)}, 10) == (900.0, 4)
    profile = {20: (600.0, 6), 40: (1200.0, 6)}
    assert forecast.profile_lookup(profile, 80) == (900.0, 12)
    assert forecast.profile_lookup({20: (600.0, 2)}, 80) == (None, 0)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"recent_count": 6, "latest_age_minutes": 10, "bucket_samples": 8, "horizon_minutes": 30}, "high"),
        ({"recent_count": 6, "latest_age_minutes": 10, "bucket_samples": 2, "horizon_minutes": 120}, "medium"),
        ({"recent_count": 2, "latest_age_minutes": 10, "bucket_samples": 2, "horizon_minutes": 30}, "low"),
    ],
)
def test_confidence_label(kwargs: dict[str, object], expected: str) -> None:
    assert forecast.confidence_label(**kwargs) == expected


def test_slope_is_clamped() -> None:
    recent = points_from([(-60, 0), (-30, 1200), (0, 2400)])
    assert forecast.trend_slope(recent, NOW) == forecast.MAX_SLOPE_SECONDS_PER_MINUTE


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
async def test_forecast_endpoint_thin_history_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = FakeCursor(
        ("JFK", "John F. Kennedy International Airport"),
        [(datetime.now(UTC) - timedelta(minutes=60), 600)],
    )
    monkeypatch.setattr(main.db, "pool", FakePool(cursor))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/api/airport/JFK/forecast")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["reason"] == "insufficient_history"
