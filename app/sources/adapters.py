"""Source adapters. One per verified airport feed.

Every adapter reads only publicly published data (the same JSON feeds the
airports' own public websites load in a visitor's browser), identifies with an
honest User-Agent, and polls no faster than once per minute.
"""
import json
import math
import urllib.parse
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import lzstring

from .base import FetchResult, Observation, Source

HEADERS = {"Accept": "application/json"}


def _ts(epoch: float | None) -> datetime | None:
    if not epoch:
        return None
    if epoch > 1e12:  # milliseconds
        epoch = epoch / 1000
    return datetime.fromtimestamp(epoch, tz=UTC)


def _lane(name: str) -> str:
    n = name.lower()
    if "pre" in n and ("check" in n or n.strip() in ("pre", "precheck")):
        return "precheck"
    return "standard"


# ---------------------------------------------------------------- SEA
async def fetch_sea(client: httpx.AsyncClient) -> FetchResult:
    r = await client.get("https://www.portseattle.org/api/cwt/wait-times", headers=HEADERS)
    r.raise_for_status()
    data = r.json()
    obs = []
    for cp in data:
        name = f"Checkpoint {cp['Name']}"
        wait = cp.get("WaitTimeMinutes")
        obs.append(Observation(name, "standard", wait * 60 if wait is not None else None, bool(cp.get("IsOpen"))))
    return FetchResult(raw=data, observations=obs)


# ---------------------------------------------------------------- DEN (Fruition widget API used by flydenver.com)
async def fetch_den(client: httpx.AsyncClient) -> FetchResult:
    r = await client.get(
        "https://app.flyfruition.com/api/public/tsa",
        headers={
            **HEADERS,
            "x-api-key": "vqw8ruvwqpv02pqu938bh5p028",
            "Origin": "https://www.flydenver.com",
            "Referer": "https://www.flydenver.com/",
        },
    )
    r.raise_for_status()
    data = r.json()
    obs = []
    for cp in data:
        title = cp.get("title") or "Checkpoint"
        for lane in cp.get("lanes", []) or []:
            if lane.get("hide_lane"):
                continue
            wait = _den_wait_seconds(lane.get("wait_time"))
            obs.append(
                Observation(
                    f"{title} — {lane.get('title', 'Lane')}",
                    _lane(lane.get("title") or ""),
                    wait,
                    not lane.get("closed", False) and not lane.get("force_close", False),
                )
            )
    return FetchResult(raw=data, observations=obs)


def _den_wait_seconds(wait: str | None) -> int | None:
    """DEN publishes ranges like '3-7' (minutes); use the upper bound."""
    if not wait:
        return None
    part = str(wait).split("-")[-1].strip()
    try:
        return int(part) * 60
    except ValueError:
        return None


# ---------------------------------------------------------------- Airport Labs vendor (MCO / IAH / HOU / DFW)
async def _airportlabs(client: httpx.AsyncClient, url: str, key: str, version: str, origin: str) -> FetchResult:
    r = await client.get(
        url,
        headers={
            **HEADERS,
            "api-key": key,
            "api-version": version,
            "Origin": origin,
            "Referer": origin + "/",
            "Content-Type": "application/json",
        },
    )
    r.raise_for_status()
    data = r.json()
    obs = []
    for cp in data.get("data", {}).get("wait_times", []):
        if not cp.get("isDisplayable", True):
            continue
        lane = cp.get("lane") or ""
        obs.append(
            Observation(
                cp.get("name") or "Checkpoint",
                "precheck" if "pre" in lane.lower() or "pre" in (cp.get("name") or "").lower() else "standard",
                cp.get("waitSeconds"),
                bool(cp.get("isOpen", True)),
                _ts(cp.get("lastUpdatedTimestamp")),
            )
        )
    return FetchResult(raw=data, observations=obs)


async def fetch_mco(client: httpx.AsyncClient) -> FetchResult:
    return await _airportlabs(
        client, "https://api.goaa.aero/wait-times/checkpoint/MCO",
        "8eaac7209c824616a8fe58d22268cd59", "140", "https://flymco.com",
    )


async def fetch_iah(client: httpx.AsyncClient) -> FetchResult:
    return await _airportlabs(
        client, "https://api.houstonairports.mobi/wait-times/checkpoint/iah",
        "9ACB3B733BE94B11A03B6E84CA87E895", "120", "https://www.fly2houston.com",
    )


async def fetch_hou(client: httpx.AsyncClient) -> FetchResult:
    return await _airportlabs(
        client, "https://api.houstonairports.mobi/wait-times/checkpoint/hou",
        "9ACB3B733BE94B11A03B6E84CA87E895", "120", "https://www.fly2houston.com",
    )


async def fetch_dfw(client: httpx.AsyncClient) -> FetchResult:
    return await _airportlabs(
        client, "https://api.dfwairport.mobi/wait-times/checkpoint/DFW",
        "87856E0636AA4BF282150FCBE1AD63DE", "170", "https://www.dfwairport.com",
    )


# ---------------------------------------------------------------- SLC
async def fetch_slc(client: httpx.AsyncClient) -> FetchResult:
    r = await client.get("https://slcairport.com/ajaxtsa/waittimes", headers=HEADERS)
    r.raise_for_status()
    data = r.json()
    obs = [
        Observation(
            "Main Checkpoint", "standard",
            int(data["rightnow"]) * 60 if data.get("rightnow") is not None else None,
        )
    ]
    return FetchResult(raw=data, observations=obs)


# ---------------------------------------------------------------- LAS (Zensors widget API used by harryreidairport.com)
LAS_SLUG = "t1LQGTAPA"
LAS_TOKEN = "3Ll9yq2riLZctX1CZ94FRgLcScJimgXx"
LAS_JOURNEYS = {
    "t2K25H6KA": "T1 - A/B Gates",
    "tGMD2ET8Y": "T1 - C/D Gates",
    "t0CSXP4SK": "T3 - D/E Gates",
}


async def fetch_las(client: httpx.AsyncClient) -> FetchResult:
    raws = {}
    obs = []
    for journey, jname in LAS_JOURNEYS.items():
        inp = json.dumps({"0": {"journey": journey, "slug": LAS_SLUG, "domainSlug": "LAS", "token": LAS_TOKEN}})
        url = (
            "https://embed.zensors.live/api/embeddable-widget/trpc/waitTimeExplorer.update?batch=1&input="
            + urllib.parse.quote(inp)
        )
        r = await client.get(
            url,
            headers={
                **HEADERS,
                "Referer": f"https://embed.zensors.live/LAS/{LAS_SLUG}/waitTimeExplorer?token={LAS_TOKEN}",
            },
        )
        r.raise_for_status()
        data = r.json()
        raws[journey] = data
        paths = data[0]["result"]["data"]["paths"]
        for key, p in paths.items():
            wt = p.get("waitTime") or {}
            obs.append(
                Observation(
                    f"{jname} — {p.get('name', key)}",
                    "precheck" if key == "precheck" else "standard",
                    round(float(wt["value"]) * 60) if wt.get("value") is not None else None,
                    bool(p.get("open", True)),
                    _ts(wt.get("timestamp")),
                )
            )
    return FetchResult(raw=raws, observations=obs)


# ---------------------------------------------------------------- PANYNJ (JFK / LGA / EWR)
PANYNJ_SITES = {
    "JFK": "https://www.jfkairport.com",
    "LGA": "https://www.laguardiaairport.com",
    "EWR": "https://www.newarkairport.com",
}
PANYNJ_QUERY = """
query GetSecurityWaitTimes($airportCode: String!, $terminal: String) {
  securityWaitTimes(airportCode: $airportCode, terminal: $terminal) {
    title
    terminal
    gate
    checkPoint
    queueType
    isOpen
    waitTime
    isWaitTimeAvailable
    status
    lastUpdated
  }
}
"""
PANYNJ_TZ = ZoneInfo("America/New_York")
PANYNJ_MAX_WAIT_MINUTES = 600
PANYNJ_LANE_TYPES = {
    "reg": "standard",
    "tsapre": "precheck",
}


def _panynj_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _panynj_checkpoint_name(row: dict) -> str:
    title = _panynj_text(row.get("title"))
    if not title:
        title = f"Terminal {_panynj_text(row.get('terminal'))}".strip()
    name = title or "Terminal"

    gate = _panynj_text(row.get("gate"))
    if gate and gate.lower() != "all gates":
        name += f" — Gates {gate}"

    checkpoint = _panynj_text(row.get("checkPoint"))
    if checkpoint and checkpoint.lower() not in {"main chekpoint", "main checkpoint"}:
        name += f" — {checkpoint}"
    return name[:120]


def _panynj_wait_seconds(row: dict) -> int | None:
    if not row.get("isOpen"):
        return None
    if not row.get("isWaitTimeAvailable"):
        return 0 if _panynj_text(row.get("status")).lower() == "no wait" else None

    minutes = row.get("waitTime")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return None
    if (isinstance(minutes, float) and not math.isfinite(minutes)) or minutes < 0 or minutes > PANYNJ_MAX_WAIT_MINUTES:
        return None
    return int(minutes) * 60


def _panynj_published_at(row: dict, now_et: datetime) -> datetime | None:
    text = _panynj_text(row.get("lastUpdated"))
    if not text:
        return None
    try:
        updated = datetime.strptime(text, "%I:%M %p").time()  # noqa: DTZ007
    except ValueError:
        return None

    cutoff = now_et.astimezone(UTC) + timedelta(minutes=5)
    candidates = [
        datetime.combine(
            now_et.date() - timedelta(days=days), updated, tzinfo=PANYNJ_TZ
        )
        .replace(fold=fold)
        .astimezone(UTC)
        for days in (0, 1)
        for fold in (0, 1)
    ]
    past = [c for c in candidates if c <= cutoff]
    if not past:
        return None
    return max(past)


async def _panynj(client: httpx.AsyncClient, iata: str) -> FetchResult:
    origin = PANYNJ_SITES[iata]
    payload = {
        "operationName": "GetSecurityWaitTimes",
        "variables": {"airportCode": iata},
        "query": PANYNJ_QUERY,
    }
    body = lzstring.LZString().compressToEncodedURIComponent(json.dumps(payload))
    r = await client.post(
        f"{origin}/api/graphql",
        content=body,
        headers={
            **HEADERS,
            "Content-Type": "text/plain",
            "Origin": origin,
            "Referer": origin + "/",
        },
    )
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise ValueError(f"PANYNJ {iata}: feed returned errors")

    rows = (data.get("data") or {}).get("securityWaitTimes") or []
    if not rows:
        raise ValueError(f"PANYNJ {iata}: feed returned no checkpoints")

    now_et = datetime.now(PANYNJ_TZ)
    observations = []
    for row in rows:
        queue_type = _panynj_text(row.get("queueType")).lower()
        observations.append(
            Observation(
                _panynj_checkpoint_name(row),
                PANYNJ_LANE_TYPES.get(queue_type, "other"),
                _panynj_wait_seconds(row),
                bool(row.get("isOpen")),
                _panynj_published_at(row, now_et),
            )
        )
    return FetchResult(raw=data, observations=observations)


async def fetch_jfk(client: httpx.AsyncClient) -> FetchResult:
    return await _panynj(client, "JFK")


async def fetch_lga(client: httpx.AsyncClient) -> FetchResult:
    return await _panynj(client, "LGA")


async def fetch_ewr(client: httpx.AsyncClient) -> FetchResult:
    return await _panynj(client, "EWR")


SOURCES: list[Source] = [
    Source("SEA", "Port of Seattle — SEA checkpoint wait times", "https://www.portseattle.org/sea-tac", "Port of Seattle (portseattle.org)", 120, fetch_sea),
    Source("DEN", "Denver International Airport — security wait times", "https://www.flydenver.com/security/", "Denver International Airport (flydenver.com)", 120, fetch_den),
    Source("MCO", "Orlando International Airport — checkpoint wait times", "https://flymco.com/security/", "Greater Orlando Aviation Authority (flymco.com)", 120, fetch_mco),
    Source("IAH", "Houston Airports — IAH checkpoint wait times", "https://www.fly2houston.com/iah/security", "Houston Airport System (fly2houston.com)", 120, fetch_iah),
    Source("HOU", "Houston Airports — HOU checkpoint wait times", "https://www.fly2houston.com/hou/security", "Houston Airport System (fly2houston.com)", 120, fetch_hou),
    Source("DFW", "DFW International Airport — security wait times", "https://www.dfwairport.com/security/", "DFW International Airport (dfwairport.com)", 120, fetch_dfw),
    Source("SLC", "Salt Lake City International Airport — TSA wait times", "https://slcairport.com/", "Salt Lake City Department of Airports (slcairport.com)", 120, fetch_slc),
    Source("LAS", "Harry Reid International Airport — security wait times", "https://www.harryreidairport.com/security-wait-times", "Harry Reid International Airport (harryreidairport.com)", 120, fetch_las),
    Source("JFK", "John F. Kennedy International Airport — security wait times", "https://www.jfkairport.com/", "Port Authority of New York and New Jersey (jfkairport.com)", 120, fetch_jfk),
    Source("LGA", "LaGuardia Airport — security wait times", "https://www.laguardiaairport.com/", "Port Authority of New York and New Jersey (laguardiaairport.com)", 120, fetch_lga),
    Source("EWR", "Newark Liberty International Airport — security wait times", "https://www.newarkairport.com/", "Port Authority of New York and New Jersey (newarkairport.com)", 120, fetch_ewr),
]
