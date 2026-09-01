"""httpx transport that sends through libcurl for hosts whose CDN edge rejects httpx's TLS handshake."""
from __future__ import annotations

import httpx
from curl_cffi.requests import AsyncSession

# Request headers that describe the httpx connection rather than the request; curl sets its own.
_HOP_HEADERS = {"host", "connection", "accept-encoding", "content-length"}
# Response headers describing the wire encoding curl already undid.
_ENCODING_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


class CurlTransport(httpx.AsyncBaseTransport):
    def __init__(self, timeout: float = 30) -> None:
        self._timeout = timeout
        self._session: AsyncSession | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._session is None:
            self._session = AsyncSession()
        if request.method != "GET":
            raise httpx.UnsupportedProtocol(f"CurlTransport only relays GET, not {request.method}")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
        response = await self._session.get(str(request.url), headers=headers, timeout=self._timeout)
        return httpx.Response(
            response.status_code,
            headers=[(k, v) for k, v in response.headers.items() if k.lower() not in _ENCODING_HEADERS],
            content=response.content,
            request=request,
        )

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def curl_mounts(*origins: str) -> dict[str, httpx.AsyncBaseTransport]:
    """``mounts=`` for ``httpx.AsyncClient`` routing the given origins through libcurl."""
    transport = CurlTransport()
    return {origin: transport for origin in origins}


# Origins served by an edge (Pantheon/Fastly for flysfo.com) that returns 403 to httpx's TLS fingerprint.
CURL_ORIGINS = ("https://www.flysfo.com",)
