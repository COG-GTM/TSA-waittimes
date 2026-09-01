"""Source adapters. One per verified airport feed.

Every adapter reads only publicly published data (the same JSON feeds the
airports' own public websites load in a visitor's browser), identifies with an
honest User-Agent, and polls no faster than once per minute.
"""
import json
import math
import re
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any
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


_PRECHECK_RE = re.compile(r"\bpre[\s-]?check\b|\bpre\b")
_ALT_LANE_RE = re.compile(r"\b(clear|priority|premium|premier|employee|crew|staff|kcm|fis|customs|immigration)\b")


def _lane(*parts: str | None) -> str:
    n = " ".join(p.lower() for p in parts if p)
    if _PRECHECK_RE.search(n):
        return "precheck"
    if _ALT_LANE_RE.search(n):
        return "other"
    return "standard"


def _iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 instant from a feed; returns tz-aware UTC or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else None


def _phrase_minutes(text: str | None) -> int | None:
    """Convert a wait phrase in minutes to seconds using its upper bound."""
    if not text:
        return None
    nums = re.findall(r"\d+", str(text))
    if not nums:
        return None
    return max(int(n) for n in nums) * 60

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


# ---------------------------------------------------------------- Airport Labs vendor (MCO / IAH / HOU / DFW / CLT / CVG)
async def _airportlabs(
    client: httpx.AsyncClient,
    url: str,
    key: str,
    version: str,
    origin: str,
    *,
    collection: str = "wait_times",
) -> FetchResult:
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
    for cp in data.get("data", {}).get(collection, []):
        if not cp.get("isDisplayable", True):
            continue
        lane = cp.get("lane") or ""
        name = cp.get("name") or "Checkpoint"
        attrs = cp.get("attributes")
        precheck = (
            _lane(lane, name) == "precheck"
            or (isinstance(attrs, dict) and bool(attrs.get("preCheck")))
        )
        obs.append(
            Observation(
                name,
                "precheck" if precheck else ("other" if _lane(lane, name) == "other" else "standard"),
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


async def fetch_clt(client: httpx.AsyncClient) -> FetchResult:
    return await _airportlabs(
        client, "https://api.cltairport.mobi/wait-times/checkpoint/CLT",
        "5ccb418715f9428ca6cb4df1635d4815", "130", "https://www.cltairport.com",
    )


async def fetch_cvg(client: httpx.AsyncClient) -> FetchResult:
    return await _airportlabs(
        client, "https://api.cvgairport.mobi/checkpoints/CVG",
        "b6461a439f1047ac950a920866b86fef", "100", "https://www.cvgairport.com",
        collection="checkpoints",
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


# ---------------------------------------------------------------- Zensors vendor (LAS / BOS)
LAS_SLUG = "t1LQGTAPA"
LAS_TOKEN = "3Ll9yq2riLZctX1CZ94FRgLcScJimgXx"
LAS_JOURNEYS = {
    "t2K25H6KA": "T1 - A/B Gates",
    "tGMD2ET8Y": "T1 - C/D Gates",
    "t0CSXP4SK": "T3 - D/E Gates",
}

BOS_SLUG = "tSTQVPRW1"
BOS_TOKEN = "9uBjlxUu2dTQydGHYGtoDYxH5TE0vHOl"
BOS_JOURNEYS = {
    "t6CQ1P0Y3": "Checkpoint 1: A Gates",
    "tKK3PDVP9": "Checkpoint 2: A Gates PreCheck Only",
    "tXT4B8KMX": "Checkpoint 3: Gates B1 - B22",
    "tF1JP9828": "Checkpoint 4: Gates B23 - 40",
    "tSGV88H0D": "Checkpoint 5: Terminal C",
    "tWEBCSW2Q": "Checkpoint 6: All E Gates",
    "tCLRGFHM9": "Checkpoint 7: All E Gates",
}


async def _zensors(
    client: httpx.AsyncClient, domain_slug: str, slug: str, token: str, journeys: dict[str, str]
) -> FetchResult:
    """Zensors embeddable-widget trpc API (embed.zensors.live), one call per journey."""
    raws = {}
    obs = []
    for journey, jname in journeys.items():
        inp = json.dumps({"0": {"journey": journey, "slug": slug, "domainSlug": domain_slug, "token": token}})
        url = (
            "https://embed.zensors.live/api/embeddable-widget/trpc/waitTimeExplorer.update?batch=1&input="
            + urllib.parse.quote(inp)
        )
        r = await client.get(
            url,
            headers={
                **HEADERS,
                "Referer": f"https://embed.zensors.live/{domain_slug}/{slug}/waitTimeExplorer?token={token}",
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
                    _lane(key),
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


async def fetch_las(client: httpx.AsyncClient) -> FetchResult:
    return await _zensors(client, "LAS", LAS_SLUG, LAS_TOKEN, LAS_JOURNEYS)


async def fetch_bos(client: httpx.AsyncClient) -> FetchResult:
    return await _zensors(client, "BOS", BOS_SLUG, BOS_TOKEN, BOS_JOURNEYS)


# ---------------------------------------------------------------- PIT (ACAA wait-times API used by flypittsburgh.com; Zensors-derived)
async def fetch_pit(client: httpx.AsyncClient) -> FetchResult:
    r = await client.get(
        "https://acaa-dna-api-prod.azure-api.net/tsa/wait-times",
        headers={
            **HEADERS,
            "Ocp-Apim-Subscription-Key": "92cd43f60453443098d08528bf0c994e",
            "Origin": "https://flypittsburgh.com",
            "Referer": "https://flypittsburgh.com/",
        },
    )
    r.raise_for_status()
    data = r.json()
    obs = []
    for q in data:
        if not q.get("canDisplayData", True):
            continue
        queue = q.get("queueName") or "Queue"
        wait = q.get("waitTime")
        obs.append(
            Observation(
                f"{q.get('checkpointName', 'Checkpoint')} — {queue}",
                _lane(queue),
                int(wait) * 60 if wait is not None else None,
                str(q.get("status", "")).lower() == "open",
            )
        )
    return FetchResult(raw=data, observations=obs)


async def fetch_phx(client: httpx.AsyncClient) -> FetchResult:
    r = await client.get(
        "https://api.phx.aero/avn-wait-times/raw?Key=4f85fe2ef5a240d59809b63de94ef536",
        headers={
            **HEADERS,
            "Origin": "https://www.skyharbor.com",
            "Referer": "https://www.skyharbor.com/",
        },
    )
    r.raise_for_status()
    data = r.json()
    obs = []
    for queue in data.get("current", []):
        projected = queue.get("projectedWaitTime")
        if projected is None:
            projected = (
                queue.get("projectedMaxWaitMinutes") * 60
                if queue.get("projectedMaxWaitMinutes") is not None
                else None
            )
        wait = round(float(projected)) if projected is not None else None
        name = queue["queueName"]
        obs.append(Observation(name, _lane(name), wait, True, _iso(queue.get("time"))))
    return FetchResult(raw=data, observations=obs)


async def fetch_dtw(client: httpx.AsyncClient) -> FetchResult:
    r = await client.get(
        "https://proxy.metroairport.com/SkyFiiTSAProxy.ashx",
        headers={**HEADERS, "Referer": "https://www.metroairport.com/"},
    )
    r.raise_for_status()
    data = r.json()
    obs = []
    for entry in data:
        name = entry.get("Name")
        if not name:
            continue
        wait_minutes = entry.get("WaitTime")
        wait = round(float(wait_minutes) * 60) if wait_minutes is not None else None
        obs.append(Observation(f"{name} Terminal", "standard", wait, True))
    return FetchResult(raw=data, observations=obs)


async def fetch_mia(client: httpx.AsyncClient) -> FetchResult:
    r = await client.get(
        "https://waittime.api.aero/waittime/v2/current/MIA",
        headers={
            **HEADERS,
            "x-apikey": "5d0cacea6e41416fdcde0c5c5a19d867",
            "Origin": "https://www.miami-airport.com",
            "Referer": "https://www.miami-airport.com/tsa-waittimes.asp",
        },
    )
    r.raise_for_status()
    data = r.json()
    obs = []
    cutoff = datetime.now(UTC) - timedelta(days=7)
    for queue in data.get("current", []):
        status = (queue.get("status") or "").strip().lower()
        is_open = status == "open"
        published_at = _iso(queue.get("time"))
        if published_at and published_at < cutoff:
            continue
        name = queue["queueName"]
        lane = _lane(name)
        projected = queue.get("projectedWaitTime")
        wait = round(float(projected)) if is_open and projected is not None else None
        obs.append(Observation(name, lane, wait, is_open, published_at))
    return FetchResult(raw=data, observations=obs)


async def fetch_dca(client: httpx.AsyncClient) -> FetchResult:
    r = await client.get(
        "https://www.flyreagan.com/security-wait-times",
        headers={
            **HEADERS,
            "Referer": "https://www.flyreagan.com/travel-information/security-information",
        },
    )
    r.raise_for_status()
    data = r.json()
    obs = []
    for key, entry in data.get("response", {}).get("res", {}).items():
        location = entry.get("location")
        name = f"Checkpoint {key} ({location})" if location else f"Checkpoint {key}"
        is_open = not entry.get("isDisabled")
        obs.append(
            Observation(name, "standard", _phrase_minutes(entry.get("waittime")) if is_open else None, is_open)
        )
        if "pre" in entry:
            is_open = not entry.get("pre_disabled")
            obs.append(
                Observation(name, "precheck", _phrase_minutes(entry.get("pre")) if is_open else None, is_open)
            )
    return FetchResult(raw=data, observations=obs)


async def fetch_ord(client: httpx.AsyncClient) -> FetchResult:
    r = await client.get(
        "https://tsawaittimes.flychicago.com/tsawaittimes",
        headers={
            **HEADERS,
            "Referer": "https://www.flychicago.com/ohare/travelerinfo/security/Pages/default.aspx",
        },
    )
    r.raise_for_status()
    data = r.json()
    parsed_rows: list[tuple[str, str, str, dict[str, Any]]] = []
    for row in data:
        name_parts = str(row.get("name", "")).split(".")
        if len(name_parts) < 3 or name_parts[1].lower() == "paxfacing":
            continue
        wait_value = row.get("waitTimes")
        if wait_value is None or wait_value == 424242:
            continue
        segment = name_parts[2]
        match = re.search(r"[Tt](\d+)[Cc](\d+(?:[a-zA-Z](?=[A-Z]|$))?)", segment)
        if not match:
            continue
        checkpoint_name = f"Terminal {match.group(1)} Checkpoint {match.group(2)}"
        segment_lower = segment.lower()
        if "precheck" in segment_lower:
            lane = "precheck"
        elif "general" in segment_lower or "totalwaittime" in segment_lower or "waittime" in segment_lower:
            lane = "standard"
        else:
            continue
        parsed_rows.append((checkpoint_name, lane, segment, row))

    non_overview_checkpoints = {
        checkpoint_name
        for checkpoint_name, _lane, segment, _row in parsed_rows
        if not segment.lower().startswith("overview")
    }
    selected: dict[tuple[str, str], tuple[str, str, str, dict[str, Any]]] = {}
    for checkpoint_name, lane, segment, row in parsed_rows:
        if segment.lower().startswith("overview") and checkpoint_name in non_overview_checkpoints:
            continue
        selected.setdefault((checkpoint_name, lane), (checkpoint_name, lane, segment, row))

    obs = []
    for checkpoint_name, lane, _segment, row in selected.values():
        wait_value = row.get("waitTimes")
        is_open = wait_value != 0
        wait = round(float(wait_value)) if is_open and wait_value is not None else None
        obs.append(Observation(checkpoint_name, lane, wait, is_open, _iso(row.get("t"))))
    return FetchResult(raw=data, observations=obs)


async def fetch_pdx(client: httpx.AsyncClient) -> FetchResult:
    r = await client.get(
        "https://www.flypdx.com/TSAWaitTimesRefresh",
        headers={**HEADERS, "Referer": "https://www.flypdx.com/"},
    )
    r.raise_for_status()
    data = r.json()
    obs = []
    for entry in data.get("WaitTimes", []):
        counter_name = entry.get("CounterName") or ""
        display = entry.get("DisplayText")
        try:
            wait = int(float(display) * 60) if display is not None else None
        except (TypeError, ValueError):
            wait = None
        if counter_name.startswith("North"):
            checkpoint_name = "North Checkpoint"
            is_open = not data.get("NorthCheckpointClosed", False)
        elif counter_name.startswith("South"):
            checkpoint_name = "South Checkpoint"
            is_open = not data.get("SouthCheckpointClosed", False)
        else:
            checkpoint_name = counter_name
            is_open = True
        obs.append(
            Observation(
                checkpoint_name,
                "precheck" if "precheck" in counter_name.lower() else "standard",
                wait if is_open else None,
                is_open,
            )
        )
    return FetchResult(raw=data, observations=obs)


SOURCES: list[Source] = [
    Source("SEA", "Port of Seattle — SEA checkpoint wait times", "https://www.portseattle.org/sea-tac", "Port of Seattle (portseattle.org)", 120, fetch_sea),
    Source("DEN", "Denver International Airport — security wait times", "https://www.flydenver.com/security/", "Denver International Airport (flydenver.com)", 120, fetch_den),
    Source("MCO", "Orlando International Airport — checkpoint wait times", "https://flymco.com/security/", "Greater Orlando Aviation Authority (flymco.com)", 120, fetch_mco),
    Source("IAH", "Houston Airports — IAH checkpoint wait times", "https://www.fly2houston.com/iah/security", "Houston Airport System (fly2houston.com)", 120, fetch_iah),
    Source("HOU", "Houston Airports — HOU checkpoint wait times", "https://www.fly2houston.com/hou/security", "Houston Airport System (fly2houston.com)", 120, fetch_hou),
    Source("DFW", "DFW International Airport — security wait times", "https://www.dfwairport.com/security/", "DFW International Airport (dfwairport.com)", 120, fetch_dfw),
    Source("CLT", "Charlotte Douglas International Airport — security wait times", "https://www.cltairport.com/airport-info/security/", "Charlotte Douglas International Airport (cltairport.com)", 120, fetch_clt),
    Source("CVG", "Cincinnati/Northern Kentucky International Airport — security wait times", "https://www.cvgairport.com/security/", "Kenton County Airport Board (cvgairport.com)", 120, fetch_cvg),
    Source("SLC", "Salt Lake City International Airport — TSA wait times", "https://slcairport.com/", "Salt Lake City Department of Airports (slcairport.com)", 120, fetch_slc),
    Source("LAS", "Harry Reid International Airport — security wait times", "https://www.harryreidairport.com/security-wait-times", "Harry Reid International Airport (harryreidairport.com)", 120, fetch_las),
    Source("BOS", "Boston Logan International Airport — security wait times", "https://www.massport.com/logan-airport/", "Massachusetts Port Authority (massport.com)", 120, fetch_bos),
    Source("PIT", "Pittsburgh International Airport — security wait times", "https://flypittsburgh.com/pittsburgh-international-airport/security/", "Allegheny County Airport Authority (flypittsburgh.com)", 120, fetch_pit),
    Source("JFK", "John F. Kennedy International Airport — security wait times", "https://www.jfkairport.com/", "Port Authority of New York and New Jersey (jfkairport.com)", 120, fetch_jfk),
    Source("LGA", "LaGuardia Airport — security wait times", "https://www.laguardiaairport.com/", "Port Authority of New York and New Jersey (laguardiaairport.com)", 120, fetch_lga),
    Source("EWR", "Newark Liberty International Airport — security wait times", "https://www.newarkairport.com/", "Port Authority of New York and New Jersey (newarkairport.com)", 120, fetch_ewr),
    Source("PHX", "Phoenix Sky Harbor International Airport — security wait times", "https://www.skyharbor.com/", "City of Phoenix Aviation Department (skyharbor.com)", 120, fetch_phx),
    Source("DTW", "Detroit Metropolitan Wayne County Airport — TSA wait times", "https://www.metroairport.com/", "Wayne County Airport Authority (metroairport.com)", 120, fetch_dtw),
    Source("MIA", "Miami International Airport — TSA checkpoint wait times", "https://www.miami-airport.com/tsa-waittimes.asp", "Miami-Dade Aviation Department (miami-airport.com)", 120, fetch_mia),
    Source("DCA", "Ronald Reagan Washington National Airport — security wait times", "https://www.flyreagan.com/travel-information/security-information", "Metropolitan Washington Airports Authority (flyreagan.com)", 120, fetch_dca),
    Source("ORD", "O'Hare International Airport — TSA checkpoint wait times", "https://www.flychicago.com/ohare/travelerinfo/security/Pages/default.aspx", "Chicago Department of Aviation (flychicago.com)", 120, fetch_ord),
    Source("PDX", "Portland International Airport — TSA wait times", "https://www.flypdx.com/", "Port of Portland (flypdx.com)", 120, fetch_pdx),
]
