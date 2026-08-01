"""NOAA Aviation Weather integration for anomaly context.

Queries the free NOAA Aviation Weather Center API for international
SIGMETs (significant meteorological events) near anomaly locations.
If turbulence, convective activity, or icing is reported in the vicinity,
the anomaly may be explained by weather rather than spoofing.

API: https://aviationweather.gov/api/data/isigmet
Free, no authentication required. 100 requests/minute limit.
"""

import logging
import math
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

NOAA_BASE = "https://aviationweather.gov/api/data"
CACHE_TTL = 300  # 5-minute cache for SIGMETs
SEARCH_RADIUS_NM = 100  # Search radius for relevant weather near anomaly

_sigmet_cache: tuple[float, list[dict]] = (0, [])


def _fetch_sigmets() -> list[dict]:
    """Fetch all active international SIGMETs from NOAA."""
    global _sigmet_cache
    now = time.time()

    if now - _sigmet_cache[0] < CACHE_TTL:
        return _sigmet_cache[1]

    try:
        resp = httpx.get(
            f"{NOAA_BASE}/isigmet",
            params={"format": "json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _sigmet_cache = (now, data if isinstance(data, list) else [])
        logger.info(f"Fetched {len(_sigmet_cache[1])} active international SIGMETs")
        return _sigmet_cache[1]
    except Exception as e:
        logger.warning(f"NOAA SIGMET fetch failed: {e}")
        return _sigmet_cache[1]  # return stale cache if available


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in nautical miles."""
    R = 3440  # Earth radius in nautical miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_weather_context(lat: float, lon: float, altitude_m: float = 0) -> Optional[dict]:
    """Find any active SIGMETs near the given position.

    Returns None if no relevant weather is found, or a dict with weather context.
    """
    sigmets = _fetch_sigmets()
    if not sigmets:
        return None

    relevant = []
    for sigmet in sigmets:
        try:
            props = sigmet.get("properties", sigmet) if isinstance(sigmet, dict) else {}
            geom = sigmet.get("geometry", {}) if isinstance(sigmet, dict) else {}

            # Try to get coordinates from the geometry
            coords = None
            if geom.get("type") == "Point":
                coords = geom.get("coordinates", [])
            elif geom.get("type") == "Polygon":
                coords = geom.get("coordinates", [[[]]])[0]
                # Use centroid
                if coords:
                    lats = [c[1] for c in coords]
                    lons = [c[0] for c in coords]
                    coords = [sum(lons) / len(lons), sum(lats) / len(lats)]

            if not coords or len(coords) < 2:
                continue

            sig_lon, sig_lat = coords[0], coords[1]
            dist_nm = _haversine_nm(lat, lon, sig_lat, sig_lon)

            if dist_nm <= SEARCH_RADIUS_NM:
                hazard = props.get("hazard", props.get("phenomenon", "unknown"))
                level_str = str(props.get("level", ""))
                # Try to match altitude
                if altitude_m and level_str:
                    try:
                        sig_alt_ft = float(level_str) * 100  # FL to feet
                        sig_alt_m = sig_alt_ft * 0.3048
                        alt_diff = abs(altitude_m - sig_alt_m)
                        if alt_diff < 3000:  # within 3000m
                            relevant.append({
                                "hazard": hazard,
                                "distance_nm": round(dist_nm, 1),
                                "sigmet_level": level_str,
                                "altitude_match": True,
                            })
                    except ValueError:
                        pass
                else:
                    # Can't check altitude match, include anyway
                    relevant.append({
                        "hazard": hazard,
                        "distance_nm": round(dist_nm, 1),
                        "sigmet_level": level_str,
                        "altitude_match": None,
                    })
        except Exception:
            continue

    if not relevant:
        return None

    return {
        "relevant_sigmets": relevant[:3],
        "check_time": datetime.now(timezone.utc).isoformat(),
        "source": "NOAA Aviation Weather Center",
    }


def get_weather_prompt_fragment(lat: float, lon: float, altitude_m: float = 0) -> str:
    """Build a triage-agent prompt fragment with weather context."""
    weather = get_weather_context(lat, lon, altitude_m)

    if not weather or not weather.get("relevant_sigmets"):
        return "Weather context: No active SIGMETs near this position. Weather is unlikely to explain any anomalies."

    parts = ["Weather context: Active SIGMET(s) near this anomaly position:"]
    for s in weather["relevant_sigmets"]:
        alt_note = " (altitude match)" if s.get("altitude_match") else ""
        parts.append(
            f"  - {s['hazard']} at {s['distance_nm']}nm, SIGMET level {s['sigmet_level']}{alt_note}"
        )
    parts.append(
        "Note: Weather conditions at this location may contribute to or explain position deviations. "
        "Consider downgrading severity if the anomaly pattern is consistent with weather-related turbulence or wind shear."
    )
    return "\n".join(parts)
