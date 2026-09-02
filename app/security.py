"""Security controls shared by the web app and public API.

Security headers (STIG V-220641), structured JSON audit logging (V-220635),
whitelist input validation (V-220631) and client-IP attribution for the
rate limiter live here so every route class applies the same policy.
"""
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

IATA_RE = re.compile(r"^[A-Z]{3}$")
TRUST_PROXY_CLIENT_IP = "FLY_APP_NAME" in os.environ

HSTS = "max-age=31536000; includeSubDomains"
CSP_DEFAULT = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
CSP_EMBED = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors *"
GENERIC_ERROR = {"detail": "An error occurred"}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
        }
        fields = getattr(record, "audit", None)
        if isinstance(fields, dict):
            entry.update(fields)
        else:
            entry["message"] = record.getMessage()
        return json.dumps(entry, separators=(",", ":"), default=str)


audit_logger = logging.getLogger("audit")
if not audit_logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(_JsonFormatter())
    audit_logger.addHandler(_handler)
audit_logger.setLevel(logging.INFO)
audit_logger.propagate = False


def client_ip(request: Request) -> str:
    """Best-effort client address; proxy headers are honoured only behind the Fly edge."""
    direct = request.client.host if request.client else "unknown"
    if not TRUST_PROXY_CLIENT_IP:
        return direct
    fly_ip = request.headers.get("fly-client-ip", "").strip()
    if fly_ip:
        return fly_ip
    forwarded = request.headers.get("x-forwarded-for", "")
    first_hop = forwarded.split(",", 1)[0].strip()
    return first_hop or direct


def audit(event: str, request: Request, **extra: object) -> None:
    fields: dict[str, object] = {
        "event": event,
        "path": request.url.path,
        "method": request.method,
        "client_ip": client_ip(request),
    }
    fields.update(extra)
    audit_logger.info(event, extra={"audit": fields})


def validate_iata(iata: str, request: Request | None = None) -> str | None:
    """Return the normalized code when it matches the whitelist, else None (after auditing)."""
    normalized = iata.upper()
    if IATA_RE.fullmatch(normalized):
        return normalized
    if request is not None:
        audit("validation_failure", request, param="iata", status=404)
    return None


def require_iata(iata: str, request: Request) -> str:
    normalized = validate_iata(iata, request)
    if normalized is None:
        raise HTTPException(404, "unknown airport")
    return normalized


def apply_security_headers(request: Request, response: Response) -> Response:
    is_embed = request.url.path.startswith("/embed/")
    response.headers["Strict-Transport-Security"] = HSTS
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if is_embed:
        if "X-Frame-Options" in response.headers:
            del response.headers["X-Frame-Options"]
        response.headers["Content-Security-Policy"] = CSP_EMBED
    else:
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = CSP_DEFAULT
    return response


async def security_middleware(request: Request, call_next):
    """Headers on every response, generic 5xx bodies, and audit records for 429/5xx."""
    try:
        response = await call_next(request)
    except Exception:
        logging.getLogger(__name__).exception("unhandled error serving %s", request.url.path)
        audit("server_error", request, status=500)
        response = JSONResponse(GENERIC_ERROR, status_code=500)
    else:
        if response.status_code >= 500:
            audit("server_error", request, status=response.status_code)
        elif response.status_code == 429:
            audit("rate_limited", request, status=429)
    return apply_security_headers(request, response)
