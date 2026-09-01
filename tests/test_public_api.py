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


@pytest.mark.asyncio
async def test_airport_success_and_unknown_inputs(client, patched_loaders):
    response = await client.get("/api/v1/airport/den")
    assert response.status_code == 200
    assert response.json()["data_notice"]
    assert (await client.get("/api/v1/airport/ZZZ")).status_code == 404
    assert (await client.get("/api/v1/airport/1x!")).status_code == 404


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


@pytest.mark.asyncio
async def test_rate_limit_and_reset(client, patched_loaders):
    responses = [await client.get("/api/v1/status") for _ in range(60)]
    assert all(response.status_code == 200 for response in responses)
    limited = await client.get("/api/v1/status")
    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    public_api.reset_rate_limiter()
    assert (await client.get("/api/v1/status")).status_code == 200


@pytest.mark.asyncio
async def test_api_docs(client):
    response = await client.get("/api")
    assert response.status_code == 200
    assert "/embed/DEN" in response.text
    assert "429" in response.text
