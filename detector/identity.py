"""Aircraft identity enrichment (free sources, budgeted + cached).

Priority: OpenSky aircraft DB metadata endpoint → hexdb.io fallback.
Never blocks the poll loop; soft-fail to unavailable.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 86400  # 24h
_lookups_this_cycle = 0
MAX_LOOKUPS_PER_CYCLE = 5


def reset_cycle_budget() -> None:
    global _lookups_this_cycle
    _lookups_this_cycle = 0


def get_identity(icao24: str) -> dict:
    """Return identity dict. Always has status: ok | unavailable | pending."""
    icao = (icao24 or "").strip().lower()
    if not icao or icao.startswith("syn"):
        return {
            "status": "unavailable",
            "registration": None,
            "typecode": None,
            "operator": None,
            "note": "synthetic or empty hex",
        }

    now = time.time()
    if icao in _cache:
        ts, data = _cache[icao]
        if now - ts < CACHE_TTL:
            return data

    global _lookups_this_cycle
    if _lookups_this_cycle >= MAX_LOOKUPS_PER_CYCLE:
        return {
            "status": "pending",
            "registration": None,
            "typecode": None,
            "operator": None,
            "note": "budget",
        }

    _lookups_this_cycle += 1
    data = _fetch_opensky_metadata(icao) or _fetch_hexdb(icao)
    if data is None:
        data = {
            "status": "unavailable",
            "registration": None,
            "typecode": None,
            "operator": None,
            "note": "lookup failed",
        }
    _cache[icao] = (now, data)
    return data


def _fetch_opensky_metadata(icao24: str) -> Optional[dict]:
    try:
        resp = httpx.get(
            f"https://opensky-network.org/api/metadata/aircraft/icao/{icao24}",
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        j = resp.json()
        return {
            "status": "ok",
            "registration": j.get("registration") or j.get("reg"),
            "typecode": j.get("typecode") or j.get("model"),
            "operator": j.get("operator") or j.get("owner"),
            "note": "opensky",
        }
    except Exception as e:
        logger.debug(f"OpenSky metadata fail {icao24}: {e}")
        return None


def _fetch_hexdb(icao24: str) -> Optional[dict]:
    try:
        resp = httpx.get(
            f"https://hexdb.io/api/v1/aircraft/{icao24}",
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        j = resp.json()
        return {
            "status": "ok",
            "registration": j.get("Registration") or j.get("registration"),
            "typecode": j.get("TypeCode") or j.get("ICAOTypeCode") or j.get("Type"),
            "operator": j.get("RegisteredOwners") or j.get("OperatorFlagCode"),
            "note": "hexdb",
        }
    except Exception as e:
        logger.debug(f"hexdb fail {icao24}: {e}")
        return None
