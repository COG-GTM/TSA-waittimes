"""Shared types and helpers for source adapters."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

USER_AGENT = (
    "CheckpointWaitPicture/1.0 (demonstration dashboard aggregating publicly "
    "published airport wait times; contact: public.sector@cognition.ai)"
)


@dataclass
class Observation:
    checkpoint_name: str
    lane_type: str  # 'standard' | 'precheck' | 'other'
    wait_seconds: int | None
    is_open: bool = True
    published_at: datetime | None = None


@dataclass
class FetchResult:
    raw: Any
    observations: list[Observation] = field(default_factory=list)


@dataclass
class Source:
    code: str  # airport IATA
    name: str  # human-readable source name
    url: str  # public page where the data is published
    attribution: str
    refresh_seconds: int
    fetch: Any  # async (httpx.AsyncClient) -> FetchResult
