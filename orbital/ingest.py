"""Injectable GP element backends: fixture, CelesTrak (cached), Space-Track stub."""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from orbital.models import GpElement, SourceStatus
from orbital.synthetic import iss_element

logger = logging.getLogger(__name__)

# Repo-relative data root
_DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "orbital"
_FIXTURE_PATH = _DATA_ROOT / "fixtures" / "stations.json"
_CACHE_DIR = _DATA_ROOT / "cache"

CELESTRAK_GP = "https://celestrak.org/NORAD/elements/gp.php"
DEFAULT_CACHE_TTL_S = 2 * 3600  # 2 hours


def _parse_epoch_omm(epoch_str: str) -> datetime:
    """Parse OMM EPOCH like 2024-01-01T12:00:00.000000."""
    s = epoch_str.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(epoch_str[:19], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def gp_from_omm_dict(d: dict, source: str = "fixture") -> Optional[GpElement]:
    """Build GpElement from CelesTrak/Space-Track OMM JSON object."""
    try:
        norad = int(d.get("NORAD_CAT_ID") or d.get("OBJECT_NUMBER") or 0)
        name = str(d.get("OBJECT_NAME") or d.get("OBJECT_ID") or f"NORAD-{norad}")
        # Prefer TLE lines if present
        line1 = d.get("TLE_LINE1") or d.get("line1")
        line2 = d.get("TLE_LINE2") or d.get("line2")
        if not line1 or not line2:
            # Build from mean elements via sgp4 not always available — skip
            return None
        epoch = d.get("EPOCH") or d.get("epoch")
        if isinstance(epoch, str):
            ep = _parse_epoch_omm(epoch)
        else:
            ep = datetime.now(timezone.utc)
        return GpElement(
            norad_id=norad,
            name=name.strip(),
            epoch=ep,
            line1=str(line1).strip(),
            line2=str(line2).strip(),
            source=source,
            object_id=str(d.get("OBJECT_ID") or ""),
            mean_motion=float(d.get("MEAN_MOTION") or 0.0),
            eccentricity=float(d.get("ECCENTRICITY") or 0.0),
            inclination_deg=float(d.get("INCLINATION") or 0.0),
        )
    except (TypeError, ValueError) as e:
        logger.warning("skip OMM parse: %s", e)
        return None


class SourceBackend(ABC):
    @abstractmethod
    def list_elements(self) -> list[GpElement]:
        ...

    @abstractmethod
    def status(self) -> SourceStatus:
        ...


class FixtureBackend(SourceBackend):
    """Offline fixtures + ISS fallback."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _FIXTURE_PATH

    def list_elements(self) -> list[GpElement]:
        if self.path.is_file():
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                data = data.get("elements") or data.get("data") or []
            out: list[GpElement] = []
            for item in data:
                if "line1" in item and "line2" in item:
                    ep = item.get("epoch")
                    if isinstance(ep, str):
                        epoch = _parse_epoch_omm(ep)
                    else:
                        epoch = datetime.now(timezone.utc)
                    out.append(
                        GpElement(
                            norad_id=int(item["norad_id"]),
                            name=item.get("name", ""),
                            epoch=epoch,
                            line1=item["line1"],
                            line2=item["line2"],
                            source="fixture",
                            inclination_deg=float(item.get("inclination_deg") or 0),
                            mean_motion=float(item.get("mean_motion") or 0),
                        )
                    )
                else:
                    el = gp_from_omm_dict(item, source="fixture")
                    if el:
                        out.append(el)
            if out:
                return out
        return [iss_element()]

    def status(self) -> SourceStatus:
        return SourceStatus(
            mode="FIXTURE",
            backend="fixture",
            detail=str(self.path) if self.path.is_file() else "iss_builtin",
        )


class CelesTrakBackend(SourceBackend):
    """Fetch GROUP GP JSON with 2h disk cache. Degrades on error."""

    def __init__(
        self,
        group: str = "stations",
        cache_dir: Optional[Path] = None,
        ttl_s: float = DEFAULT_CACHE_TTL_S,
        client: Optional[httpx.Client] = None,
    ):
        self.group = group
        self.cache_dir = cache_dir or _CACHE_DIR
        self.ttl_s = ttl_s
        self._client = client
        self._last_error = ""
        self._last_fetch: Optional[datetime] = None
        self._mode = "LIVE_CELESTRAK"

    def _cache_path(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / f"{self.group}.json"

    def _read_cache(self) -> Optional[list]:
        p = self._cache_path()
        if not p.is_file():
            return None
        age = time.time() - p.stat().st_mtime
        if age > self.ttl_s:
            return None
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None

    def _write_cache(self, data: list) -> None:
        p = self._cache_path()
        p.write_text(json.dumps(data))

    def list_elements(self) -> list[GpElement]:
        cached = self._read_cache()
        if cached is not None:
            self._mode = "LIVE_CELESTRAK"
            self._last_error = "cache_hit"
            return self._parse_list(cached, source="celestrak")

        url = f"{CELESTRAK_GP}?GROUP={self.group}&FORMAT=json"
        try:
            client = self._client or httpx.Client(timeout=30.0, follow_redirects=True)
            own = self._client is None
            try:
                r = client.get(url)
            finally:
                if own:
                    client.close()
            if r.status_code != 200:
                self._mode = "DEGRADED"
                self._last_error = f"http_{r.status_code}"
                logger.error("CelesTrak %s: %s", r.status_code, r.text[:200])
                return self._fallback()
            data = r.json()
            if not isinstance(data, list):
                data = [data]
            self._write_cache(data)
            self._last_fetch = datetime.now(timezone.utc)
            self._mode = "LIVE_CELESTRAK"
            self._last_error = "fetched"
            return self._parse_list(data, source="celestrak")
        except Exception as e:
            self._mode = "DEGRADED"
            self._last_error = str(e)
            logger.exception("CelesTrak fetch failed")
            return self._fallback()

    def _fallback(self) -> list[GpElement]:
        # Stale cache if any
        p = self._cache_path()
        if p.is_file():
            try:
                data = json.loads(p.read_text())
                self._last_error = f"{self._last_error};serving_stale_cache"
                return self._parse_list(data, source="celestrak_stale")
            except json.JSONDecodeError:
                pass
        return FixtureBackend().list_elements()

    def _parse_list(self, data: list, source: str) -> list[GpElement]:
        out: list[GpElement] = []
        for d in data:
            el = gp_from_omm_dict(d, source=source)
            if el:
                out.append(el)
        return out or FixtureBackend().list_elements()

    def status(self) -> SourceStatus:
        age = None
        p = self._cache_path()
        if p.is_file():
            age = time.time() - p.stat().st_mtime
        return SourceStatus(
            mode=self._mode,
            backend="celestrak",
            detail=self._last_error,
            last_fetch_utc=self._last_fetch,
            cache_age_seconds=age,
        )


class SpaceTrackStub(SourceBackend):
    """Stub for future Space-Track integration (account + 1/hr GP)."""

    def list_elements(self) -> list[GpElement]:
        raise NotImplementedError(
            "Space-Track backend not implemented in P1; use fixture or celestrak"
        )

    def status(self) -> SourceStatus:
        return SourceStatus(
            mode="DEGRADED",
            backend="space_track_stub",
            detail="not_implemented_p1",
        )


def get_backend(name: Optional[str] = None) -> SourceBackend:
    """Factory from env ORBITAL_SOURCE=fixture|celestrak (default fixture)."""
    name = (name or os.environ.get("ORBITAL_SOURCE", "fixture")).lower()
    if name in ("celestrak", "live", "live_celestrak"):
        return CelesTrakBackend()
    if name in ("space_track", "spacetrack"):
        return SpaceTrackStub()
    return FixtureBackend()
