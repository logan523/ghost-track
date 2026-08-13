"""NASA ISS CCSDS OEM (JSC TOPO) client — real ephemeris as observed track.

Primary URL (public S3, no API key):
  https://nasa-public-data.s3.amazonaws.com/iss-coords/current/ISS_OEM/ISS.OEM_J2K_EPH.txt

Frame: EME2000 / J2000-like, Earth-centered, km and km/s, UTC.
Offline: data/orbital/fixtures/iss_oem_snippet.txt (subset of a live pull).
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from orbital.models import OrbitState

logger = logging.getLogger(__name__)

NASA_ISS_OEM_URL = (
    "https://nasa-public-data.s3.amazonaws.com/iss-coords/current/"
    "ISS_OEM/ISS.OEM_J2K_EPH.txt"
)

_DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "orbital"
_FIXTURE_PATH = _DATA_ROOT / "fixtures" / "iss_oem_snippet.txt"
_CACHE_DIR = _DATA_ROOT / "cache"
_CACHE_PATH = _CACHE_DIR / "iss_oem.txt"

DEFAULT_OEM_CACHE_TTL_S = 6 * 3600  # OEM is multi-day; refresh every 6h
ISS_NORAD_ID = 25544
ISS_NAME = "ISS (ZARYA)"

_EPOCH_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
    r"([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s+"
    r"([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s+"
    r"([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s+"
    r"([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s+"
    r"([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s+"
    r"([+\-]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)\s*$"
)


@dataclass
class OemMeta:
    object_name: str = "ISS"
    object_id: str = ""
    ref_frame: str = "EME2000"
    time_system: str = "UTC"
    originator: str = ""
    creation_date: str = ""
    start_time: Optional[datetime] = None
    stop_time: Optional[datetime] = None
    source_url: str = ""
    source_mode: str = "fixture"  # live | fixture | cache
    raw_bytes: int = 0


@dataclass
class OemEphemeris:
    meta: OemMeta
    states: list[OrbitState] = field(default_factory=list)


def _parse_iso_utc(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    # OEM often has trailing fractional seconds without timezone
    if "+" not in s[10:] and s[-1] != "Z":
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # trim excess fractional digits
            if "." in s:
                head, frac = s.split(".", 1)
                frac = re.sub(r"\D.*$", "", frac)[:6]
                dt = datetime.fromisoformat(f"{head}.{frac}" if frac else head)
            else:
                dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    else:
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_oem(text: str, *, norad_id: int = ISS_NORAD_ID) -> OemEphemeris:
    """Parse CCSDS OEM v2 text into OrbitState list (EME2000 km, km/s)."""
    meta = OemMeta()
    states: list[OrbitState] = []
    in_meta = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("META_START"):
            in_meta = True
            continue
        if line.startswith("META_STOP"):
            in_meta = False
            continue
        if line.startswith("COMMENT") or line.startswith("COVARIANCE"):
            continue
        if "=" in line and not line[0].isdigit():
            key, _, val = line.partition("=")
            key, val = key.strip().upper(), val.strip()
            if key == "OBJECT_NAME":
                meta.object_name = val
            elif key == "OBJECT_ID":
                meta.object_id = val
            elif key == "REF_FRAME":
                meta.ref_frame = val
            elif key == "TIME_SYSTEM":
                meta.time_system = val
            elif key == "ORIGINATOR":
                meta.originator = val
            elif key == "CREATION_DATE":
                meta.creation_date = val
            elif key == "START_TIME":
                try:
                    meta.start_time = _parse_iso_utc(val)
                except ValueError:
                    pass
            elif key == "STOP_TIME":
                try:
                    meta.stop_time = _parse_iso_utc(val)
                except ValueError:
                    pass
            continue
        if in_meta:
            continue

        m = _EPOCH_RE.match(line)
        if not m:
            # state lines may have more than 7 tokens if optional accel present
            parts = line.split()
            if len(parts) < 7:
                continue
            try:
                t = _parse_iso_utc(parts[0])
                r = (float(parts[1]), float(parts[2]), float(parts[3]))
                v = (float(parts[4]), float(parts[5]), float(parts[6]))
            except ValueError:
                continue
        else:
            try:
                t = _parse_iso_utc(m.group(1))
                r = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
                v = (float(m.group(5)), float(m.group(6)), float(m.group(7)))
            except ValueError:
                continue

        states.append(
            OrbitState(
                time=t,
                r_km=r,
                v_km_s=v,
                norad_id=norad_id,
                valid=True,
            )
        )

    meta.raw_bytes = len(text.encode("utf-8", errors="replace"))
    return OemEphemeris(meta=meta, states=states)


_EVENT_ROW_RE = re.compile(
    r"COMMENT\s+\|\s*(?P<event>[^|]+?)\s*\|\s*(?P<tig>[^|]+?)\s*\|\s*"
    r"(?P<orb>[^|]*)\|\s*(?P<dv>[^|]*)\|\s*(?P<ha>[^|]*)\|\s*(?P<hp>[^|]*)\|"
)


def parse_oem_events(text: str) -> list[dict]:
    """Parse TOPO TRAJECTORY EVENT SUMMARY comment rows (when present)."""
    events: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("COMMENT"):
            continue
        m = _EVENT_ROW_RE.match(line)
        if not m:
            continue
        event = m.group("event").strip()
        tig = m.group("tig").strip()
        if not event or event.upper() in ("EVENT", "") or "----" in event:
            continue
        if tig.upper() in ("TIG", "GMT", ""):
            continue
        dv_raw = (m.group("dv") or "").strip()
        dv_m_s: Optional[float] = None
        try:
            # first token is often m/s
            tok = dv_raw.split()[0] if dv_raw else ""
            dv_m_s = float(tok)
        except (ValueError, IndexError):
            dv_m_s = None
        events.append(
            {
                "event": event,
                "tig": tig,
                "orbit": (m.group("orb") or "").strip(),
                "dv_raw": dv_raw,
                "dv_m_s": dv_m_s,
                "ha": (m.group("ha") or "").strip(),
                "hp": (m.group("hp") or "").strip(),
            }
        )
    return events


def load_fixture_oem(path: Optional[Path] = None) -> OemEphemeris:
    p = path or _FIXTURE_PATH
    text = p.read_text(encoding="utf-8")
    eph = parse_oem(text)
    eph.meta.source_mode = "fixture"
    eph.meta.source_url = str(p)
    return eph


def _cache_age_s(path: Path = _CACHE_PATH) -> Optional[float]:
    if not path.is_file():
        return None
    return max(0.0, time.time() - path.stat().st_mtime)


def _read_cache(ttl_s: float = DEFAULT_OEM_CACHE_TTL_S) -> Optional[OemEphemeris]:
    age = _cache_age_s()
    if age is None or age > ttl_s:
        return None
    try:
        text = _CACHE_PATH.read_text(encoding="utf-8")
        eph = parse_oem(text)
        eph.meta.source_mode = "cache"
        eph.meta.source_url = NASA_ISS_OEM_URL
        return eph
    except OSError as e:
        logger.warning("OEM cache read failed: %s", e)
        return None


def fetch_live_oem(
    *,
    url: str = NASA_ISS_OEM_URL,
    timeout_s: float = 45.0,
    write_cache: bool = True,
) -> OemEphemeris:
    """HTTP GET full NASA ISS OEM; optionally write disk cache."""
    headers = {"User-Agent": "OrbitGhost/0.2 (portfolio; real-observation residual)"}
    with httpx.Client(timeout=timeout_s, follow_redirects=True, headers=headers) as client:
        r = client.get(url)
        r.raise_for_status()
        text = r.text
    if write_cache:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(text, encoding="utf-8")
    eph = parse_oem(text)
    eph.meta.source_mode = "live"
    eph.meta.source_url = url
    return eph


def get_iss_oem(
    source: str = "auto",
    *,
    max_states: Optional[int] = None,
    stride: int = 1,
    prefer_recent_hours: Optional[float] = 6.0,
) -> OemEphemeris:
    """Load ISS OEM from live NASA, cache, or fixture.

    source:
      auto  — live → cache → fixture
      live  — force network (fail if unavailable)
      fixture — offline snippet only
      cache — disk cache only (fail if missing/stale)
    """
    env = os.environ.get("ORBITAL_OEM_SOURCE", "").strip().lower()
    if env in ("live", "fixture", "cache", "auto"):
        source = env

    eph: Optional[OemEphemeris] = None
    err: Optional[Exception] = None

    if source == "fixture":
        eph = load_fixture_oem()
    elif source == "cache":
        eph = _read_cache(ttl_s=1e12)  # accept any cache
        if eph is None:
            raise FileNotFoundError(f"No OEM cache at {_CACHE_PATH}")
    elif source == "live":
        eph = fetch_live_oem()
    else:  # auto
        try:
            eph = fetch_live_oem()
        except Exception as e:
            err = e
            logger.warning("live OEM fetch failed, trying cache: %s", e)
            eph = _read_cache()
            if eph is None:
                logger.warning("OEM cache miss; using fixture")
                eph = load_fixture_oem()
                eph.meta.source_mode = "fixture"
                if err:
                    eph.meta.originator = (
                        eph.meta.originator or "NASA/JSC"
                    ) + f" (live_fail: {type(err).__name__})"

    assert eph is not None
    states = eph.states
    if stride > 1:
        states = states[::stride]
    if prefer_recent_hours and states:
        # Prefer the first usable window near start of file (OEM is predictive
        # from near-now); if file starts in the past, take from start. Cap length.
        pass
    if max_states is not None and max_states > 0 and len(states) > max_states:
        states = states[:max_states]
    eph.states = states
    return eph


def subsample_states(
    states: list[OrbitState],
    n: int,
) -> list[OrbitState]:
    """Evenly subsample to at most n states (preserve endpoints)."""
    if n <= 0 or len(states) <= n:
        return list(states)
    if n == 1:
        return [states[0]]
    idxs = [round(i * (len(states) - 1) / (n - 1)) for i in range(n)]
    # de-dupe while preserving order
    seen: set[int] = set()
    out: list[OrbitState] = []
    for i in idxs:
        if i not in seen:
            seen.add(i)
            out.append(states[i])
    return out
