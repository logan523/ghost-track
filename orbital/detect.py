"""End-to-end residual → CUSUM detection pipeline."""

from __future__ import annotations

import logging
import math
import time
from datetime import timedelta
from typing import Any, Optional

from orbital.cusum import CUSUMConfig, ResidualCUSUM
from orbital.ingest import custody_age_hours, custody_tier
from orbital.models import DetectionResult, GpElement, ManeuverFlag, OrbitState
from orbital.propagate import propagate_series
from orbital.residual import residual_series
from orbital.synthetic import generate_clean_series, generate_maneuver_series

logger = logging.getLogger(__name__)


def teme_to_latlon_alt(state: OrbitState) -> tuple[float, float, float]:
    """Approximate geodetic lat/lon/alt from TEME r (km). Fine for viz."""
    x, y, z = state.r_km
    r = math.sqrt(x * x + y * y + z * z) + 1e-15
    lat = math.degrees(math.asin(max(-1.0, min(1.0, z / r))))
    lon = math.degrees(math.atan2(y, x))
    alt_km = r - 6378.137
    return lat, lon, alt_km


def trail_from_states(
    ref_states: list[OrbitState],
    obs_states: list[OrbitState],
    residuals,
    stride: int = 1,
) -> list[dict[str, Any]]:
    """Downsampled trail for globe: ref + obs + residual magnitude."""
    trail: list[dict[str, Any]] = []
    for i in range(0, len(ref_states), max(1, stride)):
        rs, os_ = ref_states[i], obs_states[i]
        if not rs.valid:
            continue
        rlat, rlon, ralt = teme_to_latlon_alt(rs)
        olat, olon, oalt = teme_to_latlon_alt(os_) if os_.valid else (rlat, rlon, ralt)
        mag = residuals[i].magnitude_km if i < len(residuals) else 0.0
        trail.append(
            {
                "t": rs.time.isoformat(),
                "ref": {
                    "lat": rlat,
                    "lon": rlon,
                    "alt_km": ralt,
                    "r_km": list(rs.r_km),
                },
                "obs": {
                    "lat": olat,
                    "lon": olon,
                    "alt_km": oalt,
                    "r_km": list(os_.r_km) if os_.valid else list(rs.r_km),
                },
                "residual_km": float(mag) if mag == mag else 0.0,
            }
        )
    return trail


def detect_from_series(
    ref_states,
    obs_states,
    norad_id: int,
    name: str = "",
    cusum: Optional[ResidualCUSUM] = None,
) -> DetectionResult:
    cusum = cusum or ResidualCUSUM()
    residuals = residual_series(ref_states, obs_states)
    cusum_pos, _, flags = cusum.process(residuals, norad_id=norad_id, name=name)
    # Deduplicate: keep first flag per minute bucket
    flags = _dedupe_flags(flags)
    stride = max(1, len(ref_states) // 80)
    trail = trail_from_states(ref_states, obs_states, residuals, stride=stride)
    return DetectionResult(
        norad_id=norad_id,
        name=name,
        residuals=residuals,
        flags=flags,
        flagged=len(flags) > 0,
        meta={"n_samples": len(residuals), "trail": trail, "cusum_pos": cusum_pos},
    )


def detect_synthetic(
    dv_m_s: float = 0.0,
    n: int = 120,
    cusum_config: Optional[CUSUMConfig] = None,
    el: Optional[GpElement] = None,
) -> DetectionResult:
    """Run pipeline on clean (dv=0) or maneuvered synthetic track."""
    cusum = ResidualCUSUM(cusum_config)
    if abs(dv_m_s) < 1e-12:
        el, _times, states = generate_clean_series(el=el, n=n)
        return detect_from_series(states, states, el.norad_id, el.name, cusum=cusum)

    el, _times, ref, obs = generate_maneuver_series(dv_m_s=dv_m_s, el=el, n=n)
    result = detect_from_series(ref, obs, el.norad_id, el.name, cusum=cusum)
    result.meta["dv_m_s"] = dv_m_s
    return result


def _dedupe_flags(flags: list[ManeuverFlag]) -> list[ManeuverFlag]:
    seen: set[str] = set()
    out: list[ManeuverFlag] = []
    for f in flags:
        key = f"{f.norad_id}:{f.time.isoformat()[:16]}"
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def orbit_ring(
    el: GpElement,
    n_samples: int = 64,
) -> list[dict[str, Any]]:
    """One orbital period of TEME positions for multi-track globe."""
    n_samples = max(8, min(int(n_samples), 256))
    period_s = 86400.0 / el.mean_motion if el.mean_motion and el.mean_motion > 0.5 else 5400.0
    times = [
        el.epoch + timedelta(seconds=period_s * i / n_samples) for i in range(n_samples + 1)
    ]
    try:
        states = propagate_series(el, times)
    except Exception as e:
        logger.warning("orbit_ring failed norad=%s: %s", el.norad_id, e)
        return []
    out: list[dict[str, Any]] = []
    for s in states:
        if not s.valid:
            continue
        out.append({"r_km": list(s.r_km), "t": s.time.isoformat()})
    return out


def field_scan(
    elements: list[GpElement],
    *,
    orbit_samples: int = 48,
    include_orbits: bool = True,
) -> dict[str, Any]:
    """Scan catalog: custody age + coarse orbits. No fake residual on clean self-ref."""
    t0 = time.perf_counter()
    objects: list[dict[str, Any]] = []
    n_flagged = 0
    n_prop_err = 0
    for el in elements:
        age_h = custody_age_hours(el.epoch)
        tier = custody_tier(age_h)
        flagged = tier == "stale"
        severity = 0.0
        reason = "ok"
        if tier == "aging":
            severity = 0.35
            reason = "aging_tle"
        elif tier == "stale":
            severity = min(1.0, 0.55 + (age_h - 72.0) / 168.0 * 0.45)
            reason = "stale_tle"
            n_flagged += 1

        orbit: list[dict[str, Any]] = []
        if include_orbits:
            orbit = orbit_ring(el, n_samples=orbit_samples)
            if not orbit:
                n_prop_err += 1
                if not flagged:
                    flagged = True
                    n_flagged += 1
                severity = max(severity, 0.7)
                reason = "sgp4_error"

        objects.append(
            {
                "norad_id": el.norad_id,
                "name": el.name,
                "source": el.source,
                "inclination_deg": el.inclination_deg,
                "mean_motion": el.mean_motion,
                "epoch": el.epoch.isoformat(),
                "custody_age_hours": round(age_h, 2),
                "custody_tier": tier,
                "flagged": flagged,
                "severity": round(severity, 3),
                "reason": reason,
                "orbit": orbit if include_orbits else None,
            }
        )

    return {
        "n_scanned": len(objects),
        "n_flagged": n_flagged,
        "n_prop_error": n_prop_err,
        "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
        "objects": objects,
    }
