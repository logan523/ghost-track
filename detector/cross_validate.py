"""Multi-source cross-validation for anomaly verification.

Queries independent ADS-B aggregators (airplanes.live) to cross-check
aircraft positions. If two independent sources disagree on an aircraft's
position by >2km, that's strong evidence of spoofing. If they agree,
the anomaly is more likely a sensor transient.

This is NOT true multilateration/TDOA — it's second-source corroboration.
"""

import logging
import math
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

AIRPLANES_LIVE_BASE = "https://api.airplanes.live/v2"
CACHE_TTL = 60  # cache cross-check results for 60 seconds
DISCREPANCY_THRESHOLD_M = 2000  # 2km — positions disagreeing by more than this = probable spoofing
CORROBORATION_THRESHOLD_M = 500  # 500m — positions within this = strong agreement

# In-memory cache: {icao24: (timestamp, data)}
_cache: dict[str, tuple[float, dict]] = {}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters."""
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def cross_check_aircraft(icao24: str, opensky_lat: float, opensky_lon: float) -> Optional[dict]:
    """Query airplanes.live for an aircraft and compare positions.

    Returns None if the cross-check couldn't be performed (API down, aircraft not found).
    Returns a dict with comparison results otherwise.
    """
    # Check cache
    if icao24 in _cache:
        ts, data = _cache[icao24]
        if time.time() - ts < CACHE_TTL:
            return data

    try:
        resp = httpx.get(
            f"{AIRPLANES_LIVE_BASE}/hex/{icao24}",
            timeout=10,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

        aircraft = data.get("ac", [])
        if not aircraft:
            result = {
                "source": "airplanes.live",
                "aircraft_found": False,
                "conclusion": "aircraft_not_seen",
                "confidence": "low",
                "note": "Aircraft not visible on airplanes.live. Could be outside receiver coverage or using anonymized address.",
            }
        else:
            ac = aircraft[0]
            cross_lat = ac.get("lat")
            cross_lon = ac.get("lon")

            if cross_lat is None or cross_lon is None:
                result = {
                    "source": "airplanes.live",
                    "aircraft_found": True,
                    "has_position": False,
                    "conclusion": "no_position_data",
                    "confidence": "low",
                    "note": "Aircraft visible on airplanes.live but no position data available.",
                }
            else:
                distance_m = _haversine_m(opensky_lat, opensky_lon, cross_lat, cross_lon)

                if distance_m > DISCREPANCY_THRESHOLD_M:
                    conclusion = "probable_spoofing"
                    confidence = "high"
                    note = (
                        f"Positions disagree by {distance_m:.0f}m (>2km threshold). "
                        "Two independent ADS-B sources report different positions for the same aircraft. "
                        "This is strong evidence of GNSS spoofing or identity falsification."
                    )
                elif distance_m > CORROBORATION_THRESHOLD_M:
                    conclusion = "minor_discrepancy"
                    confidence = "medium"
                    note = (
                        f"Positions disagree by {distance_m:.0f}m (within 2km but >500m). "
                        "Could be timing differences between receiver networks or minor position error."
                    )
                else:
                    conclusion = "positions_corroborated"
                    confidence = "high"
                    note = (
                        f"Positions agree within {distance_m:.0f}m (<500m). "
                        "Two independent sources confirm the same position. The anomaly is likely a sensor transient or equipment issue, not spoofing."
                    )

                result = {
                    "source": "airplanes.live",
                    "aircraft_found": True,
                    "has_position": True,
                    "cross_lat": cross_lat,
                    "cross_lon": cross_lon,
                    "distance_m": round(distance_m, 1),
                    "conclusion": conclusion,
                    "confidence": confidence,
                    "note": note,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

        _cache[icao24] = (time.time(), result)
        return result

    except Exception as e:
        logger.warning(f"airplanes.live cross-check failed for {icao24}: {e}")
        return {
            "source": "airplanes.live",
            "aircraft_found": False,
            "conclusion": "cross_check_unavailable",
            "confidence": "low",
            "note": f"Cross-check unavailable: {str(e)[:100]}",
        }


def enrich_anomaly_with_cross_check(anomaly: dict) -> dict:
    """Add cross-validation result to an anomaly dict."""
    icao = anomaly.get("icao24", "")
    lat = anomaly.get("latitude", 0)
    lon = anomaly.get("longitude", 0)

    if icao and lat and lon:
        result = cross_check_aircraft(icao, lat, lon)
        if result:
            anomaly["cross_check"] = result
    return anomaly
