"""Injectable GP element backends: fixture, CelesTrak (cached), Space-Track stub."""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from orbital.models import GpElement, SourceStatus
from orbital.synthetic import iss_element

logger = logging.getLogger(__name__)

_DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "orbital"
_FIXTURE_PATH = _DATA_ROOT / "fixtures" / "stations.json"
_CACHE_DIR = _DATA_ROOT / "cache"

CELESTRAK_GP = "https://celestrak.org/NORAD/elements/gp.php"
DEFAULT_CACHE_TTL_S = 2 * 3600  # 2 hours

# Public CelesTrak groups we support (allowlist)
ALLOWED_GROUPS = ("stations", "visual", "active", "starlink")
DEFAULT_GROUP = "stations"
DEFAULT_MAX_OBJECTS = 300
GROUP_CAPS = {
    "stations": 500,
    "visual": 300,
    "active": 300,
    "starlink": 300,
}

# Custody age thresholds (hours since TLE epoch)
CUSTODY_FRESH_H = 24.0
CUSTODY_AGING_H = 72.0


def max_objects_cap(group: str = DEFAULT_GROUP) -> int:
    env = os.environ.get("ORBITAL_MAX_OBJECTS")
    if env:
        try:
            return max(1, min(int(env), 2000))
        except ValueError:
            pass
    return GROUP_CAPS.get(group, DEFAULT_MAX_OBJECTS)


def custody_age_hours(epoch: datetime, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=timezone.utc)
    return max(0.0, (now - epoch.astimezone(timezone.utc)).total_seconds() / 3600.0)


def custody_tier(age_h: float) -> str:
    if age_h <= CUSTODY_FRESH_H:
        return "fresh"
    if age_h <= CUSTODY_AGING_H:
        return "aging"
    return "stale"


def _parse_epoch_omm(epoch_str: str) -> datetime:
    s = epoch_str.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(epoch_str[:19], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def epoch_from_tle_line1(line1: str) -> datetime:
    """Parse epoch from classic TLE line 1 columns 19–32."""
    yy = int(line1[18:20])
    year = 2000 + yy if yy < 57 else 1900 + yy
    day = float(line1[20:32])
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1)


def parse_tle_blob(text: str, source: str = "celestrak") -> list[GpElement]:
    """Parse CelesTrak 3LE / 2LE text into GpElements."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out: list[GpElement] = []
    i = 0
    while i < len(lines):
        name = ""
        l1 = ""
        l2 = ""
        if lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            l1, l2 = lines[i], lines[i + 1]
            i += 2
        elif (
            i + 2 < len(lines)
            and lines[i + 1].startswith("1 ")
            and lines[i + 2].startswith("2 ")
        ):
            name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
            i += 3
        else:
            i += 1
            continue
        try:
            norad = int(l1[2:7])
            if not name:
                name = f"NORAD-{norad}"
            epoch = epoch_from_tle_line1(l1)
            inc = float(l2[8:16])
            mm = float(l2[52:63])
            out.append(
                GpElement(
                    norad_id=norad,
                    name=name,
                    epoch=epoch,
                    line1=l1.strip(),
                    line2=l2.strip(),
                    source=source,
                    inclination_deg=inc,
                    mean_motion=mm,
                )
            )
        except (ValueError, IndexError) as e:
            logger.warning("skip TLE parse: %s", e)
            continue
    return out


def gp_from_omm_dict(d: dict, source: str = "fixture") -> Optional[GpElement]:
    """Build GpElement from OMM JSON (requires TLE lines if present)."""
    try:
        norad = int(d.get("NORAD_CAT_ID") or d.get("OBJECT_NUMBER") or 0)
        name = str(d.get("OBJECT_NAME") or d.get("OBJECT_ID") or f"NORAD-{norad}")
        line1 = d.get("TLE_LINE1") or d.get("line1")
        line2 = d.get("TLE_LINE2") or d.get("line2")
        if not line1 or not line2:
            return None
        epoch = d.get("EPOCH") or d.get("epoch")
        if isinstance(epoch, str):
            ep = _parse_epoch_omm(epoch)
        else:
            try:
                ep = epoch_from_tle_line1(str(line1))
            except (ValueError, IndexError):
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


def apply_limit(els: list[GpElement], limit: Optional[int], group: str = DEFAULT_GROUP) -> list[GpElement]:
    cap = max_objects_cap(group)
    n = cap if limit is None else max(1, min(int(limit), cap))
    return els[:n]


class SourceBackend(ABC):
    @abstractmethod
    def list_elements(
        self, group: Optional[str] = None, limit: Optional[int] = None
    ) -> list[GpElement]:
        ...

    @abstractmethod
    def status(self) -> SourceStatus:
        ...


class FixtureBackend(SourceBackend):
    """Offline multi-object fixtures + ISS fallback."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _FIXTURE_PATH

    def list_elements(
        self, group: Optional[str] = None, limit: Optional[int] = None
    ) -> list[GpElement]:
        group = group or DEFAULT_GROUP
        out: list[GpElement] = []
        if self.path.is_file():
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                data = data.get("elements") or data.get("data") or []
            for item in data:
                if "line1" in item and "line2" in item:
                    ep = item.get("epoch")
                    if isinstance(ep, str):
                        epoch = _parse_epoch_omm(ep)
                    else:
                        try:
                            epoch = epoch_from_tle_line1(item["line1"])
                        except (ValueError, IndexError):
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
        if not out:
            out = [iss_element()]
        return apply_limit(out, limit, group)

    def status(self) -> SourceStatus:
        return SourceStatus(
            mode="FIXTURE",
            backend="fixture",
            detail=str(self.path) if self.path.is_file() else "iss_builtin",
        )


class CelesTrakBackend(SourceBackend):
    """Fetch GROUP as classic TLE (with 2h disk cache). Degrades on error.

    Note: FORMAT=json OMM from CelesTrak does NOT include TLE lines, so we
    always use FORMAT=tle and parse 3LE text.
    """

    def __init__(
        self,
        group: str = DEFAULT_GROUP,
        cache_dir: Optional[Path] = None,
        ttl_s: float = DEFAULT_CACHE_TTL_S,
        client: Optional[httpx.Client] = None,
    ):
        self.group = group if group in ALLOWED_GROUPS else DEFAULT_GROUP
        self.cache_dir = cache_dir or _CACHE_DIR
        self.ttl_s = ttl_s
        self._client = client
        self._last_error = ""
        self._last_fetch: Optional[datetime] = None
        self._mode = "LIVE_CELESTRAK"
        self._active_group = self.group

    def _cache_path(self, group: str) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / f"{group}.tle"

    def _read_cache(self, group: str) -> Optional[str]:
        p = self._cache_path(group)
        if not p.is_file():
            return None
        age = time.time() - p.stat().st_mtime
        if age > self.ttl_s:
            return None
        try:
            return p.read_text()
        except OSError:
            return None

    def _write_cache(self, group: str, text: str) -> None:
        p = self._cache_path(group)
        p.write_text(text)

    def list_elements(
        self, group: Optional[str] = None, limit: Optional[int] = None
    ) -> list[GpElement]:
        g = group if group in ALLOWED_GROUPS else self.group
        self._active_group = g

        cached = self._read_cache(g)
        if cached is not None:
            self._mode = "LIVE_CELESTRAK"
            self._last_error = "cache_hit"
            els = parse_tle_blob(cached, source="celestrak")
            if els:
                return apply_limit(els, limit, g)

        url = f"{CELESTRAK_GP}?GROUP={g}&FORMAT=tle"
        try:
            client = self._client or httpx.Client(timeout=45.0, follow_redirects=True)
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
                return apply_limit(self._fallback(g), limit, g)
            text = r.text
            self._write_cache(g, text)
            self._last_fetch = datetime.now(timezone.utc)
            self._mode = "LIVE_CELESTRAK"
            self._last_error = "fetched"
            els = parse_tle_blob(text, source="celestrak")
            if not els:
                self._mode = "DEGRADED"
                self._last_error = "empty_parse"
                return apply_limit(self._fallback(g), limit, g)
            return apply_limit(els, limit, g)
        except Exception as e:
            self._mode = "DEGRADED"
            self._last_error = str(e)
            logger.exception("CelesTrak fetch failed")
            return apply_limit(self._fallback(g), limit, g)

    def _fallback(self, group: str) -> list[GpElement]:
        p = self._cache_path(group)
        if p.is_file():
            try:
                text = p.read_text()
                self._last_error = f"{self._last_error};serving_stale_cache"
                els = parse_tle_blob(text, source="celestrak_stale")
                if els:
                    return els
            except OSError:
                pass
        return FixtureBackend().list_elements(group=group)

    def status(self) -> SourceStatus:
        age = None
        p = self._cache_path(self._active_group)
        if p.is_file():
            age = time.time() - p.stat().st_mtime
        return SourceStatus(
            mode=self._mode,
            backend="celestrak",
            detail=f"{self._last_error};group={self._active_group}",
            last_fetch_utc=self._last_fetch,
            cache_age_seconds=age,
        )


class SpaceTrackStub(SourceBackend):
    """Stub for future Space-Track integration (account + 1/hr GP)."""

    def list_elements(
        self, group: Optional[str] = None, limit: Optional[int] = None
    ) -> list[GpElement]:
        raise NotImplementedError(
            "Space-Track backend not implemented; use fixture or celestrak"
        )

    def status(self) -> SourceStatus:
        return SourceStatus(
            mode="DEGRADED",
            backend="space_track_stub",
            detail="not_implemented",
        )


def get_backend(name: Optional[str] = None) -> SourceBackend:
    """Factory from env ORBITAL_SOURCE=fixture|celestrak.

    Default is **celestrak** (live public catalog). Tests should set
    ORBITAL_SOURCE=fixture for offline determinism.
    """
    name = (name or os.environ.get("ORBITAL_SOURCE", "celestrak")).lower()
    if name in ("fixture", "offline", "test"):
        return FixtureBackend()
    if name in ("space_track", "spacetrack"):
        return SpaceTrackStub()
    # celestrak | live | live_celestrak | default
    return CelesTrakBackend()
