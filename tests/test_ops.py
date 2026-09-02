import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import httpx
import pytest
import pytest_asyncio

from app import main, ops

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def test_classify_status() -> None:
    assert ops.classify_status(NOW - timedelta(seconds=100), 0, 120, now=NOW) == "green"
    assert ops.classify_status(NOW - timedelta(seconds=300), 0, 120, now=NOW) == "amber"
    assert ops.classify_status(NOW - timedelta(minutes=30), 0, 120, now=NOW) == "red"
    assert ops.classify_status(NOW - timedelta(seconds=10), 2, 120, now=NOW) == "red"
    assert ops.classify_status(None, 0, 120, now=NOW) == "red"
    assert ops.classify_status(NOW - timedelta(seconds=240), 0, 120, now=NOW) == "amber"


def test_backoff_seconds() -> None:
    assert ops.backoff_seconds(120, 0) == 120
    assert ops.backoff_seconds(120, 1) == 240
    assert ops.backoff_seconds(120, 4) == ops.poller.MAX_BACKOFF
    assert ops.backoff_seconds(120, 10) == ops.poller.MAX_BACKOFF


def test_truncate_error() -> None:
    assert ops.truncate_error("short") == "short"
    assert ops.truncate_error("x" * 161) == "x" * 160 + "…"
    assert ops.truncate_error(None) is None


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.query = ""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, query: str, _params: tuple[Any, ...] = ()) -> None:
        self.query = query
        self.connection.queries.append(query)

    async def fetchall(self) -> list[Any]:
        if "FROM sources s" in self.query:
            if self.connection.fail_sources:
                raise RuntimeError("source inventory unavailable")
            return [
                ("AAA", "Good source", 120, NOW - timedelta(seconds=100),
                 NOW - timedelta(seconds=50), None, None, 0),
                ("VENDOR", "Broken source", 300, NOW - timedelta(minutes=5),
                 NOW - timedelta(seconds=30), "x" * 200, NOW - timedelta(seconds=30), 2),
            ]
        if "FROM observations" in self.query and "GROUP BY 1" in self.query:
            return [("AAA", 12), ("VENDOR", 4)]
        if "DISTINCT ON (checkpoint_id)" in self.query:
            return [(True, NOW - timedelta(minutes=5)), (False, NOW - timedelta(minutes=2))]
        if "count(DISTINCT c.airport_iata)" in self.query:
            return [(2,)]
        if self.query == "SELECT count(*) FROM observations":
            return [(100,)]
        if self.query == "SELECT count(*) FROM observations_hourly":
            return [(20,)]
        if self.query == "SELECT count(*) FROM raw_payloads":
            return [(50,)]
        if "max(updated_at)" in self.query:
            return [(NOW - timedelta(minutes=1),)]
        if "pg_database_size" in self.query:
            if self.connection.fail_db_size:
                raise RuntimeError("database unavailable")
            return [(4096,)]
        if "faa_airport_events" in self.query:
            return [(3, NOW - timedelta(minutes=2))]
        if "weather_alerts" in self.query:
            return [(4, NOW - timedelta(minutes=3))]
        if "tsa_throughput" in self.query:
            return [(6, NOW.date(), NOW - timedelta(minutes=4))]
        raise AssertionError(f"unexpected query: {self.query}")


class FakeConnection:
    def __init__(self, *, fail_db_size: bool = False, fail_sources: bool = False) -> None:
        self.fail_db_size = fail_db_size
        self.fail_sources = fail_sources
        self.queries: list[str] = []
        self.rollback_count = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    async def rollback(self) -> None:
        self.rollback_count += 1

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection_value = connection

    def connection(self, *, timeout: float | None = None) -> FakeConnection:
        return self.connection_value


@pytest.mark.asyncio
async def test_build_ops_is_resilient_to_query_failure() -> None:
    connection = FakeConnection(fail_db_size=True)
    payload = await ops.build_ops(connection, now=NOW)

    assert set(payload) == {
        "generated_at", "sources", "sources_available", "system", "data_sources", "status_counts",
    }
    assert payload["sources_available"] is True
    assert payload["sources"][0]["status"] == "green"
    assert payload["sources"][1]["status"] == "red"
    assert payload["sources"][1]["estimated_backoff_seconds"] == ops.poller.MAX_BACKOFF
    assert payload["sources"][1]["last_error"] == "x" * 160 + "…"
    assert payload["system"]["db_size_bytes"] is None
    assert payload["system"]["observations_rows"] == 100
    assert payload["data_sources"]["tsa_throughput"]["latest_date"] == "2026-01-01"
    assert connection.rollback_count == 1


@pytest.mark.asyncio
async def test_build_ops_reports_last_cleanup_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ops.poller, "LAST_CLEANUP_AT", NOW)

    payload = await ops.build_ops(FakeConnection(), now=NOW)

    assert payload["system"]["last_cleanup_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_build_ops_handles_source_inventory_failure() -> None:
    connection = FakeConnection(fail_sources=True)
    payload = await ops.build_ops(connection, now=NOW)

    assert payload["sources"] == []
    assert payload["sources_available"] is False
    assert payload["status_counts"] == {"green": None, "amber": None, "red": None}
    assert payload["system"]["observations_rows"] == 100
    assert payload["system"]["db_size_bytes"] == 4096
    assert connection.rollback_count == 1


@pytest.fixture
def api_pool(monkeypatch: pytest.MonkeyPatch) -> FakeConnection:
    connection = FakeConnection()
    monkeypatch.setattr(main.db, "pool", FakePool(connection))
    return connection


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_api_ops_endpoint_shape(client, api_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    canned = ops.empty_payload(NOW)

    async def build_canned(_conn: Any, *, now: datetime) -> dict[str, Any]:
        return canned

    monkeypatch.setattr(main.ops, "build_ops", build_canned)

    response = await client.get("/api/ops")

    assert response.status_code == 200
    assert set(response.json()) == {
        "generated_at", "sources", "sources_available", "system", "data_sources", "status_counts",
    }
    assert response.json()["sources_available"] is False


@pytest.mark.asyncio
async def test_api_ops_endpoint_fallback(client, api_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    async def raise_snapshot(_conn: Any, *, now: datetime) -> dict[str, Any]:
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(main.ops, "build_ops", raise_snapshot)
    response = await client.get("/api/ops")

    assert response.status_code == 200
    assert response.json()["sources"] == []
    assert response.json()["sources_available"] is False


@pytest.mark.asyncio
async def test_api_ops_endpoint_timeout_returns_empty_payload(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hanging_snapshot(_now: datetime) -> dict[str, Any]:
        await asyncio.sleep(60)
        return {}

    monkeypatch.setattr(main, "_ops_snapshot", hanging_snapshot)
    monkeypatch.setattr(main, "OPS_TOTAL_TIMEOUT", 0.01)

    response = await client.get("/api/ops")

    assert response.status_code == 200
    assert response.json()["sources"] == []
    assert response.json()["sources_available"] is False


@pytest.mark.asyncio
async def test_healthz_db_failure_returns_degraded_status(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_snapshot() -> dict[str, Any]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main, "_healthz_snapshot", failed_snapshot)

    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "detail": "database unavailable",
    }


@pytest.mark.asyncio
async def test_healthz_timeout_returns_degraded_status(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hanging_snapshot() -> dict[str, Any]:
        await asyncio.sleep(60)
        return {}

    monkeypatch.setattr(main, "_healthz_snapshot", hanging_snapshot)
    monkeypatch.setattr(main, "HEALTHZ_TIMEOUT", 0.01)

    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


@pytest.mark.asyncio
async def test_ops_page(client) -> None:
    response = await client.get("/ops")

    assert response.status_code == 200
    assert 'name="robots"' in response.text
    assert 'id="ops-sources"' in response.text
    assert "/static/ops.js" in response.text
