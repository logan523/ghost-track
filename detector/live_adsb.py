"""Free live ADS-B ingestion — no API key required.

Priority order for each region poll:
  1. OpenSky Network /states/all (bbox) — public, rate-limited
  2. adsb.lol point query — rich identity (reg, type, squawk)
  3. airplanes.live point query — same family

Returns StateVector list with registration/type when the feed provides them.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

import httpx

from detector.models import StateVector

logger = logging.getLogger(__name__)

ADSB_LOL = "https://api.adsb.lol/v2"
AIRPLANES_LIVE = "https://api.airplanes.live/v2"
OPENSKY = "https://opensky-network.org/api"


def _bbox_center_radius_nm(min_lat, max_lat, min_lon, max_lon) -> tuple[float, float, int]:
    lat = (min_lat + max_lat) / 2
    lon = (min_lon + max_lon) / 2
    # half-diagonal approx in nm
    dlat = (max_lat - min_lat) / 2
    dlon = (max_lon - min_lon) / 2 * math.cos(math.radians(lat))
    r_nm = int(math.sqrt(dlat * dlat + dlon * dlon) * 60 * 1.15) + 40
    r_nm = max(80, min(r_nm, 450))
    return lat, lon, r_nm


def _in_bbox(lat, lon, min_lat, max_lat, min_lon, max_lon) -> bool:
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def _alt_ft_to_m(alt) -> float:
    if alt is None or alt == "ground":
        return float("nan")
    try:
        return float(alt) * 0.3048
    except (TypeError, ValueError):
        return float("nan")


def _gs_kt_to_ms(gs) -> float:
    if gs is None:
        return float("nan")
    try:
        return float(gs) * 0.514444
    except (TypeError, ValueError):
        return float("nan")


def _rate_fpm_to_ms(rate) -> float:
    if rate is None:
        return float("nan")
    try:
        return float(rate) * 0.00508
    except (TypeError, ValueError):
        return float("nan")


def parse_adsb_exchange_style(
    ac_list: list,
    region: str,
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    source: str,
) -> list[StateVector]:
    """Parse airplanes.live / adsb.lol aircraft list into StateVectors."""
    now = datetime.now(timezone.utc)
    out: list[StateVector] = []
    for ac in ac_list or []:
        try:
            lat = ac.get("lat")
            lon = ac.get("lon")
            if lat is None or lon is None:
                continue
            lat, lon = float(lat), float(lon)
            if not _in_bbox(lat, lon, min_lat, max_lat, min_lon, max_lon):
                continue
            icao = str(ac.get("hex") or "").strip().lower()
            if not icao:
                continue
            callsign = (ac.get("flight") or ac.get("callsign") or "").strip()
            track = ac.get("track")
            if track is None:
                track = ac.get("true_heading") or ac.get("mag_heading")
            on_ground = bool(ac.get("alt_baro") == "ground" or ac.get("ground"))
            alt_baro = ac.get("alt_baro")
            if alt_baro == "ground":
                alt_m = 0.0
                on_ground = True
            else:
                alt_m = _alt_ft_to_m(alt_baro)
            geo = _alt_ft_to_m(ac.get("alt_geom"))
            seen = ac.get("seen_pos") or ac.get("seen") or 0
            try:
                age = float(seen)
            except (TypeError, ValueError):
                age = 0
            # stamp roughly now - seen
            from datetime import timedelta
            ts = now - timedelta(seconds=max(0, age))

            # stash identity on object via extra attributes used by server
            sv = StateVector(
                icao24=icao,
                callsign=callsign,
                time=ts,
                latitude=lat,
                longitude=lon,
                altitude=alt_m,
                velocity=_gs_kt_to_ms(ac.get("gs")),
                heading=float(track) if track is not None else float("nan"),
                vertical_rate=_rate_fpm_to_ms(ac.get("baro_rate") or ac.get("geom_rate")),
                on_ground=on_ground,
                region=region,
                source=source,
                squawk=str(ac.get("squawk") or "").strip(),
                geo_altitude=geo,
                origin_country="",
            )
            # identity sidecar (not on dataclass fields for reg/type)
            sv._registration = (ac.get("r") or ac.get("registration") or "").strip() or None  # type: ignore
            sv._typecode = (ac.get("t") or ac.get("type") or "").strip() or None  # type: ignore
            sv._desc = (ac.get("desc") or "").strip() or None  # type: ignore
            out.append(sv)
        except (TypeError, ValueError, KeyError) as e:
            logger.debug("skip ac: %s", e)
            continue
    return out


async def fetch_opensky_bbox(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    region: str,
    headers: Optional[dict] = None,
) -> list[StateVector]:
    from detector.ingestion import _parse_state_vectors

    params = {
        "lamin": min_lat,
        "lamax": max_lat,
        "lomin": min_lon,
        "lomax": max_lon,
    }
    async with httpx.AsyncClient(timeout=20) as http:
        resp = await http.get(
            f"{OPENSKY}/states/all",
            params=params,
            headers=headers or {},
        )
        resp.raise_for_status()
        data = resp.json()
        states = _parse_state_vectors(data.get("states") or [])
        for s in states:
            s.region = region
            s.source = "opensky"
        return states


async def fetch_adsb_lol(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    region: str,
) -> list[StateVector]:
    lat, lon, r_nm = _bbox_center_radius_nm(min_lat, max_lat, min_lon, max_lon)
    url = f"{ADSB_LOL}/lat/{lat:.3f}/lon/{lon:.3f}/dist/{r_nm}"
    async with httpx.AsyncClient(timeout=20) as http:
        resp = await http.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        ac = data.get("ac") or data.get("aircraft") or []
        return parse_adsb_exchange_style(
            ac, region, min_lat, max_lat, min_lon, max_lon, "adsb.lol"
        )


async def fetch_airplanes_live(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    region: str,
) -> list[StateVector]:
    lat, lon, r_nm = _bbox_center_radius_nm(min_lat, max_lat, min_lon, max_lon)
    url = f"{AIRPLANES_LIVE}/point/{lat:.3f}/{lon:.3f}/{r_nm}"
    async with httpx.AsyncClient(timeout=20) as http:
        resp = await http.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        ac = data.get("ac") or []
        return parse_adsb_exchange_style(
            ac, region, min_lat, max_lat, min_lon, max_lon, "airplanes.live"
        )


async def fetch_live_region(
    region: str,
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    opensky_headers: Optional[dict] = None,
) -> tuple[list[StateVector], str]:
    """Return (states, source_name). Raises if all sources fail."""
    errors: list[str] = []

    # Prefer rich free feed first (identity fields); OpenSky as backup
    sources = (
        ("adsb.lol", lambda: fetch_adsb_lol(min_lat, max_lat, min_lon, max_lon, region)),
        ("airplanes.live", lambda: fetch_airplanes_live(min_lat, max_lat, min_lon, max_lon, region)),
        ("opensky", lambda: fetch_opensky_bbox(min_lat, max_lat, min_lon, max_lon, region, opensky_headers)),
    )
    for name, factory in sources:
        try:
            states = await factory()
            if states:
                logger.info("[%s] live source=%s aircraft=%d", region, name, len(states))
                return states, name
            errors.append(f"{name}: empty")
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.warning("[%s] live source %s failed: %s", region, name, e)

    raise RuntimeError("; ".join(errors) or "no live source")
