"""Fetch TSA's public daily checkpoint travel numbers (tsa.gov/travel/passenger-volumes)."""
import re
from datetime import UTC, date, datetime, timedelta

from curl_cffi.requests import AsyncSession

PAGE = "https://www.tsa.gov/travel/passenger-volumes"
FIRST_YEAR = 2019
ROW_RE = re.compile(r'text-align-center">\s*(\d{1,2}/\d{1,2}/\d{4})\s*<.*?text-align-center">\s*([\d,]+)\s*<', re.DOTALL)


def parse_throughput_html(html: str) -> list[tuple[date, int]]:
    """Parse valid daily throughput rows from a TSA passenger volumes page."""
    today = datetime.now(UTC).date()
    latest = today + timedelta(days=1)
    rows: list[tuple[date, int]] = []
    for date_text, travelers_text in ROW_RE.findall(html):
        parsed = datetime.strptime(date_text, "%m/%d/%Y").replace(tzinfo=UTC).date()
        travelers = int(travelers_text.replace(",", ""))
        if not date(2019, 1, 1) <= parsed <= latest:
            continue
        if travelers <= 0 or travelers > 10_000_000:
            continue
        rows.append((parsed, travelers))
    return rows


async def fetch_tsa_throughput(_client) -> list[tuple[date, int]]:
    """Returns (date, travelers) rows for the current year page and last year's page."""
    rows: list[tuple[date, int]] = []
    async with AsyncSession(impersonate="chrome") as s:
        for url in (PAGE, f"{PAGE}/{datetime.now(UTC).year - 1}"):
            r = await s.get(url, timeout=30)
            r.raise_for_status()
            rows.extend(parse_throughput_html(r.text))
    return rows


async def fetch_tsa_year(year: int) -> list[tuple[date, int]]:
    """Fetch and parse one complete TSA calendar year."""
    async with AsyncSession(impersonate="chrome") as s:
        response = await s.get(f"{PAGE}/{year}", timeout=30)
        response.raise_for_status()
    return [(date, travelers) for date, travelers in parse_throughput_html(response.text) if date.year == year]
