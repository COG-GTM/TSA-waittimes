"""Security headers, audit logging, input validation and client-IP attribution."""
import json
import logging

import httpx
import pytest
import pytest_asyncio
from fastapi import Request

from app import public_api, security
from app.main import app as main_app
from tests.test_public_api import AIRPORTS, DETAIL, STATUS

STRICT_HEADERS = {
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
}


@pytest.fixture(autouse=True)
def reset_rate_limit() -> None:
    public_api.reset_rate_limiter()


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=main_app, raise_app_exceptions=False)
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

    monkeypatch.setattr(public_api, "load_airports", load_airports)
    monkeypatch.setattr(public_api, "load_airport", load_airport)
    monkeypatch.setattr(public_api, "load_status", load_status)
    monkeypatch.setattr(public_api, "load_embed", load_airport)


@pytest.fixture
def audit_records(monkeypatch):
    records: list[dict] = []
    formatter = security._JsonFormatter()

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(json.loads(formatter.format(record)))

    handler = Capture()
    security.audit_logger.addHandler(handler)
    yield records
    security.audit_logger.removeHandler(handler)


def _assert_strict_headers(response: httpx.Response) -> None:
    for name, value in STRICT_HEADERS.items():
        assert response.headers.get(name) == value, name
    csp = response.headers["content-security-policy"]
    assert csp.startswith("default-src 'self'")
    assert "frame-ancestors 'none'" in csp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/", 200),
        ("/api", 200),
        ("/airport/DEN", 200),
        ("/api/v1/airports", 200),
        ("/api/v1/airport/DEN", 200),
        ("/api/v1/status", 200),
        ("/api/v1/airport/ZZZ", 404),
        ("/api/v1/nope", 404),
        ("/api/airport/bad-code", 404),
        ("/airport/bad-code", 404),
        ("/no-such-page", 404),
        ("/static/does-not-exist.js", 404),
        ("/static/app.js", 200),
    ],
)
async def test_security_headers_on_every_route_class(client, patched_loaders, path, expected_status):
    response = await client.get(path)
    assert response.status_code == expected_status
    _assert_strict_headers(response)


@pytest.mark.asyncio
async def test_embed_is_frameable_but_otherwise_hardened(client, patched_loaders):
    response = await client.get("/embed/DEN")
    assert response.status_code == 200
    assert "x-frame-options" not in response.headers
    assert response.headers["content-security-policy"] == security.CSP_EMBED
    assert response.headers["strict-transport-security"] == STRICT_HEADERS["strict-transport-security"]
    assert response.headers["x-content-type-options"] == "nosniff"
    missing = await client.get("/embed/ZZZ")
    assert missing.status_code == 404
    assert "x-frame-options" not in missing.headers


@pytest.mark.asyncio
async def test_canonical_redirect_carries_headers(client):
    response = await client.get("/airport/DEN?x=1", headers={"Host": "www.tsadelays.com"})
    assert response.status_code == 301
    assert response.headers["location"] == "https://waitpicture.com/airport/DEN?x=1"
    _assert_strict_headers(response)


@pytest.mark.asyncio
async def test_rate_limit_rejection_is_audited_with_client_ip(client, patched_loaders, audit_records, monkeypatch):
    monkeypatch.setattr(security, "TRUST_PROXY_CLIENT_IP", True)
    headers = {"Fly-Client-IP": "203.0.113.10"}
    for _ in range(public_api.RATE_LIMIT):
        assert (await client.get("/api/v1/status", headers=headers)).status_code == 200
    limited = await client.get("/api/v1/status", headers=headers)
    assert limited.status_code == 429
    _assert_strict_headers(limited)
    events = [record for record in audit_records if record["event"] == "rate_limited"]
    assert len(events) == 1
    assert events[0]["client_ip"] == "203.0.113.10"
    assert events[0]["path"] == "/api/v1/status"
    assert events[0]["status"] == 429
    assert events[0]["timestamp"].endswith("+00:00")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/api/airport/1x!", "/api/airport/bad-code/typical", "/api/airport/bad-code/forecast",
     "/api/v1/airport/1x!", "/embed/1x!", "/airport/bad-code"],
)
async def test_invalid_iata_is_rejected_generically_and_audited(client, patched_loaders, audit_records, path):
    response = await client.get(path)
    assert response.status_code == 404
    assert "bad-code" not in response.text
    assert "1x!" not in response.text
    failures = [record for record in audit_records if record["event"] == "validation_failure"]
    assert len(failures) == 1
    assert failures[0]["path"] == path
    assert failures[0]["param"] == "iata"
    assert "bad-code" not in json.dumps(failures[0]).replace(path, "")


@pytest.mark.asyncio
async def test_unhandled_error_is_generic_and_audited(client, patched_loaders, audit_records, monkeypatch):
    async def boom():
        raise RuntimeError("database password is hunter2")

    monkeypatch.setattr(public_api, "load_status", boom)
    response = await client.get("/api/v1/status")
    assert response.status_code == 500
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    _assert_strict_headers(response)
    errors = [record for record in audit_records if record["event"] == "server_error"]
    assert len(errors) == 1
    assert errors[0]["path"] == "/api/v1/status"
    assert errors[0]["status"] == 500
    assert "hunter2" not in json.dumps(errors[0])


@pytest.mark.asyncio
async def test_embed_shares_rate_limiter(client, patched_loaders, audit_records):
    for _ in range(public_api.RATE_LIMIT):
        assert (await client.get("/embed/DEN")).status_code == 200
    limited = await client.get("/embed/DEN")
    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    assert "x-frame-options" not in limited.headers
    assert any(record["event"] == "rate_limited" and record["path"] == "/embed/DEN" for record in audit_records)


def _request(headers: dict[str, str], client_host: str = "10.0.0.1") -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": raw_headers, "client": (client_host, 1234),
    })


def test_client_ip_ignores_proxy_headers_when_not_behind_fly(monkeypatch):
    monkeypatch.setattr(security, "TRUST_PROXY_CLIENT_IP", False)
    request = _request({"Fly-Client-IP": "203.0.113.10", "X-Forwarded-For": "198.51.100.7, 10.0.0.2"})
    assert security.client_ip(request) == "10.0.0.1"


def test_client_ip_prefers_fly_header_then_first_forwarded_hop(monkeypatch):
    monkeypatch.setattr(security, "TRUST_PROXY_CLIENT_IP", True)
    both = _request({"Fly-Client-IP": "203.0.113.10", "X-Forwarded-For": "198.51.100.7, 10.0.0.2"})
    assert security.client_ip(both) == "203.0.113.10"
    forwarded_only = _request({"X-Forwarded-For": " 198.51.100.7 , 10.0.0.2"})
    assert security.client_ip(forwarded_only) == "198.51.100.7"
    assert security.client_ip(_request({"X-Forwarded-For": ""})) == "10.0.0.1"
    assert security.client_ip(_request({})) == "10.0.0.1"


def test_json_formatter_emits_single_line_json():
    record = logging.LogRecord("audit", logging.INFO, __file__, 1, "rate_limited", None, None)
    record.audit = {"event": "rate_limited", "path": "/api/v1/status", "client_ip": "203.0.113.10"}
    line = security._JsonFormatter().format(record)
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["event"] == "rate_limited"
    assert parsed["level"] == "INFO"
    assert set(parsed) == {"timestamp", "level", "event", "path", "client_ip"}
