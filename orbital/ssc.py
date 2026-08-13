"""NASA SSCWeb (Satellite Situation Center) optional multi-mission ephemeris.

Public REST (no key for many endpoints):
  https://sscweb.gsfc.nasa.gov/WS/sscr/2/

Used as fallback / multi-object path when ISS OEM is unavailable or when
querying other missions. Positions returned in GSE/GEO/etc. depending on
coordinate system request — we request GEO (geocentric equatorial) km.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree as ET

import httpx

from orbital.models import OrbitState

logger = logging.getLogger(__name__)

SSC_BASE = "https://sscweb.gsfc.nasa.gov/WS/sscr/2"
# ISS SSC identifier
DEFAULT_ISS_SSC_ID = "iss"


def _ensure_utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _fmt_ssc_time(t: datetime) -> str:
    t = _ensure_utc(t)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_ssc_locations(
    satellite: str = DEFAULT_ISS_SSC_ID,
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    resolution_minutes: int = 4,
    timeout_s: float = 60.0,
    norad_id: int = 25544,
) -> list[OrbitState]:
    """Fetch satellite locations from SSCWeb REST (XML).

    Returns OrbitState with r_km in GEO-like km if available; v zeroed
    (SSC location endpoint often omits velocity — residual uses position only).
    """
    end = _ensure_utc(end or datetime.now(timezone.utc))
    start = _ensure_utc(start or (end - timedelta(hours=2)))
    # SSC path: /locations/{satellites}/{startTime},{endTime}/[resolution]/
    path = (
        f"{SSC_BASE}/locations/{satellite}/"
        f"{_fmt_ssc_time(start)},{_fmt_ssc_time(end)}/"
        f"{max(1, resolution_minutes)}"
    )
    headers = {
        "Accept": "application/xml",
        "User-Agent": "OrbitGhost/0.2 (portfolio; SSCWeb residual)",
    }
    with httpx.Client(timeout=timeout_s, follow_redirects=True, headers=headers) as client:
        r = client.get(path)
        r.raise_for_status()
        body = r.text

    return _parse_ssc_locations_xml(body, norad_id=norad_id)


def _local(tag: str) -> str:
    """Match tags with or without namespace."""
    return tag


def _parse_ssc_locations_xml(xml_text: str, norad_id: int = 25544) -> list[OrbitState]:
    """Best-effort parse of SSCWeb locations XML into OrbitStates."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"SSC XML parse error: {e}") from e

    # Strip namespaces for simpler walking
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]

    states: list[OrbitState] = []
    # Common shapes: Data/Satellite/Coordinates or TimeSeries
    times: list[datetime] = []
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []

    for time_el in root.iter("Time"):
        try:
            times.append(
                datetime.fromisoformat(time_el.text.replace("Z", "+00:00"))
                if time_el.text
                else datetime.now(timezone.utc)
            )
        except (ValueError, AttributeError, TypeError):
            continue

    # Coordinate components often nested as X/Y/Z under Coordinate or GSE/GEO
    def collect_component(name: str) -> list[float]:
        out: list[float] = []
        for el in root.iter(name):
            if el.text and el.text.strip():
                try:
                    # multi-value whitespace separated
                    parts = el.text.split()
                    if len(parts) > 1:
                        out.extend(float(p) for p in parts)
                    else:
                        out.append(float(el.text))
                except ValueError:
                    pass
        return out

    xs = collect_component("X")
    ys = collect_component("Y")
    zs = collect_component("Z")

    n = min(len(xs), len(ys), len(zs))
    if n == 0:
        logger.warning("SSC parse: no coordinate triples found")
        return []

    if not times:
        t0 = datetime.now(timezone.utc)
        times = [t0 + timedelta(minutes=4 * i) for i in range(n)]
    n = min(n, len(times))

    for i in range(n):
        t = times[i]
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        # SSC often returns km already for GEO
        states.append(
            OrbitState(
                time=t,
                r_km=(xs[i], ys[i], zs[i]),
                v_km_s=(0.0, 0.0, 0.0),
                norad_id=norad_id,
                valid=True,
            )
        )
    return states


def ssc_available(timeout_s: float = 8.0) -> bool:
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            r = client.get(f"{SSC_BASE}/observatories")
            return r.status_code < 500
    except Exception:
        return False
