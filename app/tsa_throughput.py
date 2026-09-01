"""Fetch TSA's public daily checkpoint travel numbers (tsa.gov/travel/passenger-volumes)."""
import re
from datetime import UTC, date, datetime

from curl_cffi.requests import AsyncSession

PAGE = "https://www.tsa.gov/travel/passenger-volumes"
ROW_RE = re.compile(r'text-align-center">\s*(\d{1,2}/\d{1,2}/\d{4})\s*<.*?text-align-center">\s*([\d,]+)\s*<', re.DOTALL)


async def fetch_tsa_throughput(_client) -> list[tuple[date, int]]:
    """Returns (date, travelers) rows for the current year page and last year's page."""
    rows: list[tuple[date, int]] = []
    async with AsyncSession(impersonate="chrome") as s:
        for url in (PAGE, f"{PAGE}/{datetime.now(UTC).year - 1}"):
            r = await s.get(url, timeout=30)
            r.raise_for_status()
            for d, n in ROW_RE.findall(r.text):
                parsed = datetime.strptime(d, "%m/%d/%Y").replace(tzinfo=UTC)
                rows.append((parsed.date(), int(n.replace(",", ""))))
    return rows
