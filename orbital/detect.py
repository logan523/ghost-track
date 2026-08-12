"""End-to-end residual → CUSUM detection pipeline."""

from __future__ import annotations

import math
from typing import Any, Optional

from orbital.cusum import CUSUMConfig, ResidualCUSUM
from orbital.models import DetectionResult, GpElement, ManeuverFlag, OrbitState
from orbital.residual import residual_series
from orbital.synthetic import generate_clean_series, generate_maneuver_series


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
    _, _, flags = cusum.process(residuals, norad_id=norad_id, name=name)
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
        meta={"n_samples": len(residuals), "trail": trail},
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
