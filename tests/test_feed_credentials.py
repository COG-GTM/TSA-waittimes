"""Feed credentials come from the environment and never from source.

With every credential variable unset, an adapter must either raise
``MissingCredentialError`` before sending anything, or send requests that carry
no credential at all. A request that still authenticates means a key has been
hardcoded again.
"""
from __future__ import annotations

import asyncio
import urllib.parse

import httpx
import pytest

from app.sources import credentials
from app.sources.adapters import SOURCES
from app.sources.base import Source
from app.sources.credentials import FEED_CREDENTIALS, MissingCredentialError

CREDENTIAL_HEADERS = {"authorization", "x-api-key", "api-key", "x-apikey", "ocp-apim-subscription-key"}
CREDENTIAL_QUERY_PARAMS = {"key", "apikey", "api_key", "api-key", "token", "access_token"}


class RequestSent(Exception):
    pass


def _fetch_without_credentials(source: Source) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise RequestSent

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await source.fetch(client)

    with pytest.raises((RequestSent, MissingCredentialError)):
        asyncio.run(go())
    return sent


def _query_carries_credential(request: httpx.Request) -> bool:
    query = urllib.parse.parse_qs(request.url.query.decode())
    if CREDENTIAL_QUERY_PARAMS & {name.lower() for name in query}:
        return True
    return '"token"' in urllib.parse.unquote(request.url.query.decode())


@pytest.fixture
def no_feed_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in FEED_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("no_feed_credentials")
@pytest.mark.parametrize("source", SOURCES, ids=[s.code for s in SOURCES])
def test_no_request_authenticates_without_environment_credentials(source: Source) -> None:
    for request in _fetch_without_credentials(source):
        leaked = CREDENTIAL_HEADERS & {name.lower() for name in request.headers}
        assert not leaked, f"{source.code} sent {sorted(leaked)} without a configured credential"
        assert not _query_carries_credential(request), f"{source.code} sent a credential in the query string"


@pytest.mark.usefixtures("no_feed_credentials")
def test_missing_credential_fails_closed_before_any_request() -> None:
    by_code = {s.code: s for s in SOURCES}
    for code in ("DEN", "MCO", "IAH", "HOU", "DFW", "CLT", "CVG", "LAS", "BOS", "PIT", "PHX", "MIA"):
        assert _fetch_without_credentials(by_code[code]) == [], f"{code} sent a request without its credential"


@pytest.mark.usefixtures("no_feed_credentials")
def test_feed_credential_reports_missing_and_blank_values(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(MissingCredentialError, match="FEED_KEY_DEN"):
        credentials.feed_credential("FEED_KEY_DEN")
    monkeypatch.setenv("FEED_KEY_DEN", "   ")
    with pytest.raises(MissingCredentialError):
        credentials.feed_credential("FEED_KEY_DEN")
    monkeypatch.setenv("FEED_KEY_DEN", " k1 ")
    assert credentials.feed_credential("FEED_KEY_DEN") == "k1"
    assert "FEED_KEY_DEN" not in credentials.missing_feed_credentials()
    assert "FEED_KEY_MIA" in credentials.missing_feed_credentials()
    with pytest.raises(KeyError):
        credentials.feed_credential("FEED_KEY_NOT_A_SOURCE")


def test_redact_credentials_rewrites_recorded_values() -> None:
    from tests.harness import FIXTURE_CREDENTIALS, redact_credentials

    exchanges = [{"method": "GET", "url": "https://x.test/raw?Key=live-secret", "status": 200, "json": {}}]
    redacted = redact_credentials(exchanges, {"FEED_KEY_PHX": "live-secret", "FEED_KEY_MIA": ""})
    assert redacted[0]["url"] == f"https://x.test/raw?Key={FIXTURE_CREDENTIALS['FEED_KEY_PHX']}"
    assert exchanges[0]["url"].endswith("live-secret")
