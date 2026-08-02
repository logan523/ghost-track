"""OpenSky Network REST API client with injectable backend.

Implements OAuth2 client-credentials flow, token refresh, quota tracking,
coverage gap detection, and local response caching.

When OPENSKY_USERNAME/OPENSKY_PASSWORD are not set, falls back to a synthetic
data generator so the full pipeline runs without real credentials.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from config import config
from detector.models import AircraftTrack, StateVector

logger = logging.getLogger(__name__)


class OpenSkyClient:
    """OpenSky REST API client with OAuth2 auth and quota management."""

    def __init__(self, cache_dir: Optional[str] = None):
        self.base_url = config.opensky_base_url
        self.client_id = config.opensky_client_id
        self.client_secret = config.opensky_client_secret
        self.cache_dir = Path(cache_dir or config.raw_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._credits_used: int = 0
        self._daily_limit: int = 4000
        self._last_request_time: float = 0.0
        self._min_interval: float = 1.0  # min seconds between requests

    @property
    def has_credentials(self) -> bool:
        return config.has_opensky_creds

    # ── auth ──────────────────────────────────────────────────────────

    def _obtain_token(self) -> str:
        """OAuth2 client-credentials flow. Tokens expire after ~30 minutes."""
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        logger.info("Obtaining new OpenSky OAuth2 token")
        resp = httpx.post(
            "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 1800)
        logger.info(f"Token obtained, expires in {data.get('expires_in', 1800)}s")
        return self._token

    def _headers(self) -> dict:
        if not self.has_credentials:
            return {}
        return {"Authorization": f"Bearer {self._obtain_token()}"}

    # ── rate limiting & quota ─────────────────────────────────────────

    def _throttle(self):
        """Enforce minimum interval between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def _check_quota(self, credits: int = 1):
        if self._credits_used + credits > self._daily_limit:
            logger.warning(
                f"Approaching daily credit limit ({self._credits_used}/{self._daily_limit})"
            )

    # ── caching ───────────────────────────────────────────────────────

    def _cache_path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace("?", "_").replace("&", "_")
        return self.cache_dir / f"{safe}.json"

    def _cache_get(self, key: str) -> Optional[dict]:
        p = self._cache_path(key)
        if p.exists():
            data = json.loads(p.read_text())
            age = time.time() - data.get("_cached_at", 0)
            if age < 3600:  # 1-hour cache validity
                logger.debug(f"Cache hit: {key} (age={age:.0f}s)")
                return data
        return None

    def _cache_put(self, key: str, data: dict):
        data["_cached_at"] = time.time()
        self._cache_path(key).write_text(json.dumps(data, default=str))

    # ── public API ────────────────────────────────────────────────────

    def get_states_by_bbox(
        self,
        time_start: datetime,
        time_end: datetime,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
    ) -> list[StateVector]:
        """
        Fetch state vectors within a bounding box and time range.

        Uses OpenSky's /api/states/all endpoint with historical extensions
        (available to registered users with valid token).
        """
        if not self.has_credentials:
            logger.info("No OpenSky credentials — using synthetic data")
            return _generate_synthetic_states(
                time_start, time_end, min_lat, max_lat, min_lon, max_lon
            )

        cache_key = f"states_{time_start.isoformat()}_{time_end.isoformat()}_{min_lat}_{max_lat}_{min_lon}_{max_lon}"
        cached = self._cache_get(cache_key)
        if cached:
            return _parse_state_vectors(cached.get("states", []))

        self._throttle()
        self._check_quota()

        # OpenSky historical API: /api/states/all?time=<unix>&icao24=...
        # For historical windows we fetch in 5-second buckets
        all_states: list[StateVector] = []
        window_start = int(time_start.timestamp())
        window_end = int(time_end.timestamp())

        for t in range(window_start, window_end, 5):
            try:
                resp = httpx.get(
                    f"{self.base_url}/states/all",
                    headers=self._headers(),
                    params={"time": t},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                states = _parse_state_vectors(data.get("states", []))
                # Filter to bounding box
                filtered = [
                    s
                    for s in states
                    if min_lat <= s.latitude <= max_lat
                    and min_lon <= s.longitude <= max_lon
                ]
                all_states.extend(filtered)
                self._credits_used += 1
            except httpx.HTTPError as e:
                logger.error(f"Request failed for time={t}: {e}")
                continue

        self._cache_put(cache_key, {"states": _serialize_states(all_states)})
        logger.info(
            f"Fetched {len(all_states)} state vectors for bbox [{min_lat},{max_lat},{min_lon},{max_lon}]"
        )
        return all_states

    def get_tracks_by_bbox(
        self,
        time_start: datetime,
        time_end: datetime,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
    ) -> list[AircraftTrack]:
        """Fetch state vectors and group into per-aircraft tracks."""
        states = self.get_states_by_bbox(
            time_start, time_end, min_lat, max_lat, min_lon, max_lon
        )
        return _group_into_tracks(states)

    def detect_coverage_gaps(
        self, tracks: list[AircraftTrack], max_gap_seconds: float = 30.0
    ) -> list[dict]:
        """Detect and log coverage gaps in aircraft tracks.

        Gaps between "1 hour ago" and daily bulk endpoints are permanent
        data gaps if the service has downtime — log them explicitly.
        """
        gaps = []
        for track in tracks:
            sorted_states = sorted(track.states, key=lambda s: s.time)
            for i in range(1, len(sorted_states)):
                gap = (sorted_states[i].time - sorted_states[i - 1].time).total_seconds()
                if gap > max_gap_seconds:
                    gap_info = {
                        "icao24": track.icao24,
                        "gap_start": sorted_states[i - 1].time.isoformat(),
                        "gap_end": sorted_states[i].time.isoformat(),
                        "duration_seconds": gap,
                        "region": _classify_region(
                            sorted_states[i].latitude, sorted_states[i].longitude
                        ),
                    }
                    gaps.append(gap_info)
                    logger.warning(
                        f"Coverage gap: {track.icao24} from {gap_info['gap_start']} "
                        f"to {gap_info['gap_end']} ({gap:.0f}s) in {gap_info['region']}"
                    )
        return gaps


# ── helper functions ──────────────────────────────────────────────────

def _parse_state_vectors(raw_states: list) -> list[StateVector]:
    """Parse OpenSky's raw state array format into StateVector objects.

    OpenSky row indices:
      0 icao24, 1 callsign, 2 origin_country, 3 time_position, 4 last_contact,
      5 lon, 6 lat, 7 baro_alt, 8 on_ground, 9 velocity, 10 true_track,
      11 vertical_rate, 12 sensors, 13 geo_alt, 14 squawk, 15 spi,
      16 position_source, 17 category
    """
    parsed = []
    for row in raw_states:
        if row is None or len(row) < 8:
            continue
        try:
            lat = float(row[6]) if row[6] is not None else None
            lon = float(row[5]) if row[5] is not None else None
            if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
                continue
            # Prefer last_contact when time_position is missing
            t_pos = row[3] if len(row) > 3 else None
            t_contact = row[4] if len(row) > 4 else None
            t_src = t_pos or t_contact
            ts = (
                datetime.fromtimestamp(t_src, tz=timezone.utc)
                if t_src
                else datetime.now(timezone.utc)
            )
            last_contact = (
                datetime.fromtimestamp(t_contact, tz=timezone.utc)
                if t_contact
                else None
            )
            sv = StateVector(
                icao24=str(row[0]).strip().lower(),
                callsign=(row[1] or "").strip() if row[1] else "",
                time=ts,
                latitude=lat,
                longitude=lon,
                altitude=float(row[7]) if row[7] is not None else float("nan"),
                velocity=float(row[9]) if len(row) > 9 and row[9] is not None else float("nan"),
                heading=float(row[10]) if len(row) > 10 and row[10] is not None else float("nan"),
                vertical_rate=float(row[11]) if len(row) > 11 and row[11] is not None else float("nan"),
                on_ground=bool(row[8]) if len(row) > 8 and row[8] is not None else False,
                origin_country=(row[2] or "").strip() if len(row) > 2 and row[2] else "",
                last_contact=last_contact,
                geo_altitude=float(row[13]) if len(row) > 13 and row[13] is not None else float("nan"),
                squawk=str(row[14]).strip() if len(row) > 14 and row[14] is not None else "",
                spi=bool(row[15]) if len(row) > 15 and row[15] is not None else False,
                position_source=int(row[16]) if len(row) > 16 and row[16] is not None else 0,
                category=int(row[17]) if len(row) > 17 and row[17] is not None else 0,
            )
            parsed.append(sv)
        except (ValueError, TypeError, IndexError) as e:
            logger.debug(f"Skipping malformed state vector: {e}")
            continue
    return parsed


def _serialize_states(states: list[StateVector]) -> list[dict]:
    """Serialize StateVector list for JSON caching."""
    return [
        {
            "icao24": s.icao24,
            "callsign": s.callsign,
            "time": s.time.isoformat(),
            "latitude": s.latitude,
            "longitude": s.longitude,
            "altitude": s.altitude,
            "velocity": s.velocity,
            "heading": s.heading,
            "vertical_rate": s.vertical_rate,
            "on_ground": s.on_ground,
        }
        for s in states
    ]


def _group_into_tracks(states: list[StateVector]) -> list[AircraftTrack]:
    """Group state vectors by ICAO24 address into per-aircraft tracks."""
    tracks: dict[str, AircraftTrack] = {}
    for sv in states:
        if sv.icao24 not in tracks:
            tracks[sv.icao24] = AircraftTrack(
                icao24=sv.icao24,
                callsign=sv.callsign,
            )
        tracks[sv.icao24].states.append(sv)
    return [t.sorted() for t in tracks.values()]


def _classify_region(lat: float, lon: float) -> str:
    """Classify a position into a named region."""
    for name, (min_lat, max_lat, min_lon, max_lon) in config.regions.items():
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return name
    return "unknown"


# ── synthetic data generator (used when no OpenSky credentials) ───────

def _generate_synthetic_states(
    time_start: datetime,
    time_end: datetime,
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
) -> list[StateVector]:
    """Generate realistic synthetic ADS-B tracks for testing.

    Creates multiple aircraft with realistic flight profiles including:
    - Normal cruise tracks (clean)
    - Tracks with injected position anomalies (simulated spoofing)
    - Tracks with slow drift (simulated GNSS jamming)
    """
    import random

    random.seed(42)
    states: list[StateVector] = []

    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2
    lat_span = max_lat - min_lat
    lon_span = max_lon - min_lon

    # Generate 8 clean aircraft, 2 with anomalies, 2 with slow drift
    aircraft_configs = [
        # (icao24, callsign, anomaly_type)
        ("A00001", "CLEAN01", "none"),
        ("A00002", "CLEAN02", "none"),
        ("A00003", "CLEAN03", "none"),
        ("A00004", "CLEAN04", "none"),
        ("A00005", "CLEAN05", "none"),
        ("A00006", "CLEAN06", "none"),
        ("A00007", "CLEAN07", "none"),
        ("A00008", "CLEAN08", "none"),
        ("A00009", "SPOOF01", "position_jump"),  # injected position jump
        ("A00010", "SPOOF02", "position_jump"),  # injected position jump
        ("A00011", "DRIFT01", "slow_drift"),  # slow GNSS drift
        ("A00012", "DRIFT02", "slow_drift"),  # slow GNSS drift
    ]

    duration = (time_end - time_start).total_seconds()
    sample_interval = 5.0  # 5-second samples like OpenSky

    for icao24, callsign, anomaly_type in aircraft_configs:
        # Random starting position within bbox (with margin)
        start_lat = center_lat + random.uniform(-0.45, 0.45) * lat_span
        start_lon = center_lon + random.uniform(-0.45, 0.45) * lon_span
        start_alt = random.uniform(8000, 12000)  # cruise altitude in meters
        heading = random.uniform(0, 360)
        speed = random.uniform(200, 280)  # m/s (~400-540 knots)
        vr = random.uniform(-2, 2)  # vertical rate m/s

        # Convert heading/speed to lat/lon rates (approximate)
        heading_rad = heading * 3.14159 / 180.0
        lat_rate = speed * 1.0 / 111320.0 * 0.001  # deg/s (approximate)
        lon_rate = speed * 1.0 / (111320.0 * 0.001)  # deg/s (simplified)

        num_samples = int(duration / sample_interval)

        # For anomaly aircraft, decide when the anomaly starts
        anomaly_start_idx = num_samples // 2 if anomaly_type != "none" else num_samples
        drift_rate = 0.0
        if anomaly_type == "slow_drift":
            drift_rate = random.uniform(0.00001, 0.0005)  # deg/s drift

        for i in range(num_samples):
            t = time_start + timedelta(seconds=i * sample_interval)
            lat = start_lat + lat_rate * i * sample_interval
            lon = start_lon + lon_rate * i * sample_interval
            alt = start_alt + vr * i * sample_interval

            # Inject anomaly
            if i >= anomaly_start_idx:
                if anomaly_type == "position_jump":
                    # Sudden position offset (ghost aircraft spoofing)
                    lat += random.uniform(0.05, 0.15) * lat_span
                    lon += random.uniform(0.05, 0.15) * lon_span
                    alt += random.uniform(500, 2000)
                elif anomaly_type == "slow_drift":
                    # Cumulative slow drift (GNSS jamming)
                    drift_samples = i - anomaly_start_idx
                    lat += drift_rate * drift_samples * sample_interval
                    lon += drift_rate * drift_samples * sample_interval * 0.5

            # Add realistic noise
            lat += random.gauss(0, 0.0001)  # ~10m GPS noise
            lon += random.gauss(0, 0.0001)

            # Clamp to valid ranges (longitude wraps, latitude clips)
            lat = max(-90.0, min(90.0, lat))
            lon = ((lon + 180.0) % 360.0) - 180.0

            states.append(
                StateVector(
                    icao24=icao24,
                    callsign=callsign,
                    time=t,
                    latitude=lat,
                    longitude=lon,
                    altitude=alt + random.gauss(0, 15),  # ~15m altitude noise
                    velocity=speed + random.gauss(0, 5),
                    heading=(heading + random.gauss(0, 2)) % 360,
                    vertical_rate=vr + random.gauss(0, 0.5),
                )
            )

    return states


# ── exports ───────────────────────────────────────────────────────────

__all__ = ["OpenSkyClient", "_generate_synthetic_states"]
