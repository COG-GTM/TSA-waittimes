import httpx
import pytest
import pytest_asyncio

from app import public_api
from app.main import app as main_app

AIRPORT = {
    "iata": "DEN",
    "name": '<script>alert(1)</script>&"',
    "city": "Denver",
    "state": "CO",
    "lat": 39.8561,
    "lon": -104.6737,
}
DETAIL = {
    "airport": AIRPORT,
    "checkpoints": [
        {
            "name": '<script>alert(1)</script>&"',
            "lane_type": "standard",
            "wait_seconds": 600,
            "is_open": True,
            "stale": False,
            "fetched_at": "2025-01-15T12:00:00+00:00",
        },
        {
            "name": "PreCheck",
            "lane_type": "precheck",
            "wait_seconds": 300,
            "is_open": True,
            "stale": False,
            "fetched_at": "2025-01-15T12:00:00+00:00",
        },
    ],
    "faa_events": [],
    "faa_attribution": "FAA National Airspace System Status (nasstatus.faa.gov)",
    "weather_alerts": [],
    "travel_period": None,
    "generated_at": "2025-01-15T12:00:00+00:00",
}
AIRPORTS = [{
    "iata": "DEN",
    "name": "Denver International Airport",
    "city": "Denver",
    "state": "CO",
    "standard_wait_seconds": 600,
    "precheck_wait_seconds": 300,
    "as_of": "2025-01-15T12:00:00+00:00",
    "last_updated": "2025-01-15T12:00:00+00:00",
    "stale": False,
    "attribution": "Example source",
    "source_url": "https://example.com/source",
}]
STATUS = {"status": "ok", "observations": 2, "sources": [{"source": "example", "healthy": True}]}


@pytest.fixture(autouse=True)
def reset_rate_limit() -> None:
    public_api.reset_rate_limiter()


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=main_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest.fixture
def patched_loaders(monkeypatch):
    async def load_airports():
        return AIRPORTS

    async def load_airport(iata: str):
        return DETAIL if iata == "DEN" else None

    async def load_status():
        return STATUS

    async def load_embed(iata: str):
        return DETAIL if iata == "DEN" else None

    monkeypatch.setattr(public_api, "load_airports", load_airports)
    monkeypatch.setattr(public_api, "load_airport", load_airport)
    monkeypatch.setattr(public_api, "load_status", load_status)
    monkeypatch.setattr(public_api, "load_embed", load_embed)


@pytest.mark.asyncio
async def test_airports_shape_and_cors(client, patched_loaders):
    response = await client.get("/api/v1/airports", headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    body = response.json()
    assert body["data_notice"]
    assert set(body["airports"][0]) == {
        "iata", "name", "city", "state", "standard_wait_seconds",
        "precheck_wait_seconds", "as_of", "last_updated", "stale",
        "attribution", "source_url",
    }
    assert response.headers["access-control-allow-origin"] == "*"
    assert "retry-after" in response.headers["access-control-expose-headers"].lower()


@pytest.mark.asyncio
async def test_airport_success_and_unknown_inputs(client, patched_loaders):
    response = await client.get("/api/v1/airport/den")
    assert response.status_code == 200
    assert response.json()["data_notice"]
    unknown = await client.get("/api/v1/airport/ZZZ")
    assert unknown.status_code == 404
    assert unknown.json()["data_notice"]
    invalid = await client.get("/api/v1/airport/1x!")
    assert invalid.status_code == 404
    assert invalid.json()["data_notice"]
    routed = await client.get("/api/v1/not-an-endpoint")
    assert routed.status_code == 404
    assert routed.json()["data_notice"]


@pytest.mark.asyncio
async def test_status_shape(client, patched_loaders):
    response = await client.get("/api/v1/status")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["data_notice"]
    assert "sources" in response.json()


@pytest.mark.asyncio
async def test_cors_is_scoped_to_public_api(client, patched_loaders):
    response = await client.get("/embed/DEN", headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_embed_escapes_content_and_allows_frames(client, patched_loaders):
    response = await client.get("/embed/DEN")
    assert response.status_code == 200
    assert "x-frame-options" not in response.headers
    assert "frame-ancestors *" in response.headers["content-security-policy"]
    assert '<script>alert(1)</script>' not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&amp;&quot;" in response.text
    status = await client.get("/api/v1/status")
    assert status.headers["x-frame-options"] == "DENY"


def test_display_wait_rounds_half_minute_up():
    checkpoints = [{
        "lane_type": "standard",
        "wait_seconds": 150,
        "is_open": True,
        "stale": False,
    }]
    assert public_api._display_wait(checkpoints, "standard") == "3"


def test_updated_time_uses_only_displayed_checkpoints():
    detail = {
        "generated_at": "2025-01-15T09:00:00+00:00",
        "checkpoints": [
            {
                "fetched_at": "2025-01-15T12:00:00+00:00",
                "is_open": False,
                "stale": False,
                "wait_seconds": 600,
            },
            {
                "fetched_at": "2025-01-15T11:00:00+00:00",
                "is_open": True,
                "stale": True,
                "wait_seconds": 600,
            },
            {
                "fetched_at": "2025-01-15T10:00:00+00:00",
                "is_open": True,
                "stale": False,
                "wait_seconds": None,
            },
            {
                "fetched_at": "2025-01-15T08:00:00+00:00",
                "is_open": True,
                "stale": False,
                "wait_seconds": 120,
            },
        ],
    }
    assert public_api._updated_time(detail) == "08:00 UTC"
    assert public_api._updated_time({
        "generated_at": "2025-01-15T09:00:00+00:00",
        "checkpoints": [{"fetched_at": "2025-01-15T12:00:00+00:00", "is_open": False, "stale": False,
                         "wait_seconds": 600}],
    }) == "09:00 UTC"


@pytest.mark.asyncio
async def test_rate_limit_and_reset(client, patched_loaders):
    responses = [await client.get("/api/v1/status") for _ in range(60)]
    assert all(response.status_code == 200 for response in responses)
    limited = await client.get("/api/v1/status")
    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    assert limited.json()["data_notice"]
    public_api.reset_rate_limiter()
    assert (await client.get("/api/v1/status")).status_code == 200


@pytest.mark.asyncio
async def test_fly_client_ips_have_independent_rate_limits(
    client, patched_loaders, monkeypatch
):
    monkeypatch.setattr(public_api, "TRUST_PROXY_CLIENT_IP", True)
    first_ip = {"Fly-Client-IP": "203.0.113.10"}
    second_ip = {"Fly-Client-IP": "203.0.113.11"}
    responses = [await client.get("/api/v1/status", headers=first_ip) for _ in range(60)]
    assert all(response.status_code == 200 for response in responses)
    assert (await client.get("/api/v1/status", headers=first_ip)).status_code == 429
    other_ip = await client.get("/api/v1/status", headers=second_ip)
    assert other_ip.status_code == 200


@pytest.mark.asyncio
async def test_direct_clients_share_rate_limit_despite_fly_headers(
    client, patched_loaders, monkeypatch
):
    monkeypatch.setattr(public_api, "TRUST_PROXY_CLIENT_IP", False)
    first_ip = {"Fly-Client-IP": "203.0.113.10"}
    second_ip = {"Fly-Client-IP": "203.0.113.11"}
    responses = [await client.get("/api/v1/status", headers=first_ip) for _ in range(60)]
    assert all(response.status_code == 200 for response in responses)
    limited = await client.get("/api/v1/status", headers=second_ip)
    assert limited.status_code == 429


@pytest.mark.asyncio
async def test_rate_limiter_prunes_expired_entries(client, patched_loaders, monkeypatch):
    monkeypatch.setattr(public_api, "TRUST_PROXY_CLIENT_IP", True)
    monkeypatch.setattr(public_api.time, "monotonic", lambda: 0.0)
    await client.get("/api/v1/status", headers={"Fly-Client-IP": "203.0.113.10"})
    await client.get("/api/v1/status", headers={"Fly-Client-IP": "203.0.113.11"})
    assert set(public_api._rate_limits) == {"203.0.113.10", "203.0.113.11"}

    monkeypatch.setattr(public_api.time, "monotonic", lambda: 61.0)
    response = await client.get("/api/v1/status", headers={"Fly-Client-IP": "203.0.113.12"})

    assert response.status_code == 200
    assert set(public_api._rate_limits) == {"203.0.113.12"}


@pytest.mark.asyncio
async def test_rate_limiter_prunes_unique_clients_at_window_boundaries(
    client, patched_loaders, monkeypatch
):
    monkeypatch.setattr(public_api, "TRUST_PROXY_CLIENT_IP", True)
    current_time = 0.0
    monkeypatch.setattr(public_api.time, "monotonic", lambda: current_time)

    for offset in range(3):
        current_time = offset * (public_api.RATE_WINDOW_SECONDS + 1)
        for client_number in range(10):
            response = await client.get(
                "/api/v1/status",
                headers={"Fly-Client-IP": f"203.0.113.{offset * 10 + client_number}"},
            )
            assert response.status_code == 200
        assert len(public_api._rate_limits) <= 10


@pytest.mark.asyncio
async def test_api_docs(client):
    response = await client.get("/api")
    assert response.status_code == 200
    assert "/embed/DEN" in response.text
    assert "429" in response.text
