"""Vendor feed credentials, supplied through the environment.

The airports' public sites hand these keys to every visitor's browser, but they
are still vendor-issued credentials and never belong in source control or git
history. Adapters read them at fetch time, so a missing key fails that one
source closed (no request is sent, the poll is recorded unhealthy) while the
rest of the app keeps running.
"""
import os

FEED_CREDENTIALS: dict[str, str] = {
    "FEED_KEY_DEN": "DEN — Fruition widget API (x-api-key)",
    "FEED_KEY_MCO": "MCO — Airport Labs (api-key)",
    "FEED_KEY_HOUSTON": "IAH/HOU — Airport Labs (api-key, shared by both airports)",
    "FEED_KEY_DFW": "DFW — Airport Labs (api-key)",
    "FEED_KEY_CLT": "CLT — Airport Labs (api-key)",
    "FEED_KEY_CVG": "CVG — Airport Labs (api-key)",
    "FEED_KEY_LAS": "LAS — Zensors embeddable widget token",
    "FEED_KEY_BOS": "BOS — Zensors embeddable widget token",
    "FEED_KEY_PIT": "PIT — ACAA Azure API Management (Ocp-Apim-Subscription-Key)",
    "FEED_KEY_PHX": "PHX — api.phx.aero (Key query parameter)",
    "FEED_KEY_MIA": "MIA — waittime.api.aero (x-apikey)",
}


class MissingCredentialError(RuntimeError):
    """A feed credential is not configured; the source fails closed."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is not configured; set it in the environment (fly secrets set {name}=...)")
        self.name = name


def feed_credential(name: str) -> str:
    if name not in FEED_CREDENTIALS:
        raise KeyError(f"unknown feed credential {name}")
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingCredentialError(name)
    return value


def missing_feed_credentials() -> list[str]:
    return [name for name in FEED_CREDENTIALS if not os.environ.get(name, "").strip()]
