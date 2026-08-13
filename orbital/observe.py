"""Dual-source residual: real NASA observations vs SGP4(TLE) reference.

Primary path:
  observed  = NASA/JSC TOPO ISS OEM (EME2000 states)
  reference = SGP4(TEME) from public TLE (CelesTrak or fixture)

Alignment: both positions mapped to approximate ECEF via Earth Rotation Angle
(no polar motion / nutation) — see frames.FRAME_DISCLOSURE.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from orbital.cusum import CUSUMConfig, ResidualCUSUM
from orbital.detect import teme_to_latlon_alt
from orbital.frames import FRAME_DISCLOSURE, inertial_to_ecef_approx
from orbital.ingest import get_backend
from orbital.models import DetectionResult, GpElement, OrbitState, ResidualSample
from orbital.oem import (
    ISS_NAME,
    ISS_NORAD_ID,
    get_iss_oem,
    parse_oem_events,
    subsample_states,
)
from orbital.propagate import propagate_series, rtn_basis
from orbital.synthetic import iss_element

logger = logging.getLogger(__name__)

# Dual-source residual is dominated by TLE along-track vs NASA OEM, not radar noise.
RESIDUAL_FLOOR_NOTE_KM = (
    "Typical ISS dual-source |r| is tens of km, almost all along-track "
    "(TLE/SGP4 vs TOPO OEM). Radial is usually <1 km. Not radar noise."
)


def _window_near_epoch(
    states: list[OrbitState],
    epoch: datetime,
    n: int,
) -> list[OrbitState]:
    """Take an n-sample window centered on the state closest to epoch."""
    if not states:
        return []
    if len(states) <= n:
        return list(states)
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=timezone.utc)
    else:
        epoch = epoch.astimezone(timezone.utc)
    # Index of state nearest epoch
    best_i = min(
        range(len(states)),
        key=lambda i: abs((states[i].time - epoch).total_seconds()),
    )
    half = n // 2
    start = max(0, best_i - half)
    end = start + n
    if end > len(states):
        end = len(states)
        start = max(0, end - n)
    window = states[start:end]
    # If window still larger than n (shouldn't), subsample evenly
    if len(window) > n:
        return subsample_states(window, n)
    return window


def _resolve_iss_element(el: Optional[GpElement] = None) -> GpElement:
    if el is not None:
        return el
    backend = get_backend()
    try:
        for e in backend.list_elements(group="stations"):
            if e.norad_id == ISS_NORAD_ID or "ISS" in e.name.upper():
                return e
    except Exception as e:
        logger.warning("catalog ISS lookup failed: %s", e)
    return iss_element()


def residual_sample_ecef(ref: OrbitState, obs: OrbitState) -> ResidualSample:
    """RTN residual after mapping both inertial positions into approx ECEF."""
    if not ref.valid or not obs.valid:
        return ResidualSample(
            time=ref.time,
            radial_km=float("nan"),
            along_track_km=float("nan"),
            cross_track_km=float("nan"),
            magnitude_km=float("nan"),
        )
    r_ref_e = np.array(inertial_to_ecef_approx(ref.r_km, ref.time), dtype=float)
    r_obs_e = np.array(inertial_to_ecef_approx(obs.r_km, obs.time), dtype=float)
    # Velocity: rotate inertial v only (omit Earth-rate transport term for RTN dir)
    v_ref_e = np.array(inertial_to_ecef_approx(ref.v_km_s, ref.time), dtype=float)
    dr = r_obs_e - r_ref_e
    basis = rtn_basis(r_ref_e, v_ref_e)
    rtn = basis.T @ dr
    mag = float(np.linalg.norm(rtn))
    return ResidualSample(
        time=ref.time,
        radial_km=float(rtn[0]),
        along_track_km=float(rtn[1]),
        cross_track_km=float(rtn[2]),
        magnitude_km=mag,
    )


def residual_series_ecef(
    ref_states: list[OrbitState],
    obs_states: list[OrbitState],
) -> list[ResidualSample]:
    if len(ref_states) != len(obs_states):
        raise ValueError(
            f"ref/obs length mismatch: {len(ref_states)} vs {len(obs_states)}"
        )
    return [residual_sample_ecef(a, b) for a, b in zip(ref_states, obs_states)]


def _demean_residuals_for_cusum(
    residuals: list[ResidualSample],
    baseline_frac: float = 0.25,
) -> list[ResidualSample]:
    """CUSUM on |r| − baseline so steady model/frame floor does not alarm.

    Baseline = median of the first baseline_frac of finite samples (min 5).
    """
    mags = np.array(
        [s.magnitude_km if math.isfinite(s.magnitude_km) else np.nan for s in residuals],
        dtype=float,
    )
    finite = mags[np.isfinite(mags)]
    if len(finite) == 0:
        return residuals
    n_base = max(5, int(len(finite) * baseline_frac))
    baseline = float(np.median(finite[:n_base]))
    out: list[ResidualSample] = []
    for s, mag in zip(residuals, mags):
        if not math.isfinite(mag):
            out.append(s)
            continue
        # Non-negative excess over floor; drops below floor → 0
        excess = max(0.0, mag - baseline)
        out.append(
            ResidualSample(
                time=s.time,
                radial_km=s.radial_km,
                along_track_km=s.along_track_km,
                cross_track_km=s.cross_track_km,
                magnitude_km=excess,
            )
        )
    return out


def detect_from_dual_source(
    ref_states: list[OrbitState],
    obs_states: list[OrbitState],
    norad_id: int,
    name: str = "",
    cusum: Optional[ResidualCUSUM] = None,
) -> DetectionResult:
    """Like detect_from_series but residual via ECEF-aligned dual frames."""
    cusum = cusum or ResidualCUSUM()
    residuals = residual_series_ecef(ref_states, obs_states)
    # CUSUM on excess over baseline floor (not absolute |r|)
    cusum_series = _demean_residuals_for_cusum(residuals)
    cusum_pos, _, flags = cusum.process(
        cusum_series, norad_id=norad_id, name=name
    )
    # Attach true residual magnitudes on flags
    mag_by_t = {s.time: s for s in residuals}
    for f in flags:
        true = mag_by_t.get(f.time)
        if true is not None:
            f.residual_magnitude_km = true.magnitude_km
            f.residual_rtn_km = (
                true.radial_km,
                true.along_track_km,
                true.cross_track_km,
            )
    # Deduplicate flags
    seen: set[str] = set()
    deduped = []
    for f in flags:
        key = f"{f.norad_id}:{f.time.isoformat()[:16]}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    flags = deduped
    stride = max(1, len(ref_states) // 80)
    trail = _trail_dual(ref_states, obs_states, residuals, stride=stride)
    baseline_mags = [
        s.magnitude_km for s in residuals if math.isfinite(s.magnitude_km)
    ]
    n_base = max(5, int(len(baseline_mags) * 0.25)) if baseline_mags else 0
    baseline = float(np.median(baseline_mags[:n_base])) if n_base else 0.0
    return DetectionResult(
        norad_id=norad_id,
        name=name,
        residuals=residuals,
        flags=flags,
        flagged=len(flags) > 0,
        meta={
            "n_samples": len(residuals),
            "trail": trail,
            "cusum_pos": cusum_pos,
            "cusum_baseline_km": baseline,
            "cusum_on": "excess_over_baseline",
        },
    )


def _trail_dual(
    ref_states: list[OrbitState],
    obs_states: list[OrbitState],
    residuals: list[ResidualSample],
    stride: int = 1,
) -> list[dict[str, Any]]:
    trail: list[dict[str, Any]] = []
    for i in range(0, len(ref_states), max(1, stride)):
        rs, os_ = ref_states[i], obs_states[i]
        if not rs.valid:
            continue
        rlat, rlon, ralt = teme_to_latlon_alt(rs)
        olat, olon, oalt = (
            teme_to_latlon_alt(os_) if os_.valid else (rlat, rlon, ralt)
        )
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


def observe_iss_residual(
    *,
    n_samples: int = 90,
    oem_source: str = "auto",
    cusum_config: Optional[CUSUMConfig] = None,
    el: Optional[GpElement] = None,
    k: Optional[float] = None,
    h: Optional[float] = None,
) -> DetectionResult:
    """NASA ISS OEM (observed) vs SGP4 TLE (reference) residual + CUSUM.

    n_samples: max OEM states used (evenly subsampled).
    oem_source: auto | live | fixture | cache
    """
    n_samples = max(10, min(int(n_samples), 500))
    el = _resolve_iss_element(el)

    # CUSUM on excess over baseline floor (see detect_from_dual_source).
    # k/h are in km of *excess* residual, not absolute |r|.
    if cusum_config is None:
        # Tuned so steady model-floor jitter (~1–3 km) does not alarm;
        # a true step or growth above the floor still does.
        cusum_config = CUSUMConfig(
            k=k if k is not None else 1.5,  # km excess per sample
            h=h if h is not None else 12.0,  # accumulated excess km
        )
    elif k is not None or h is not None:
        cusum_config = CUSUMConfig(
            k=k if k is not None else cusum_config.k,
            h=h if h is not None else cusum_config.h,
            window_samples=cusum_config.window_samples,
        )

    eph = get_iss_oem(source=oem_source, max_states=None)
    obs_raw = eph.states
    if not obs_raw:
        raise RuntimeError("OEM ephemeris empty — no observation states")

    # Prefer OEM window near TLE epoch (SGP4 accuracy degrades far from epoch).
    # Fall back to "now" then OEM start if epoch is outside the ephemeris span.
    obs_states = _window_near_epoch(obs_raw, el.epoch, n_samples)
    times = [s.time for s in obs_states]
    ref_states = propagate_series(el, times)

    # Align invalid: if SGP4 fails at a time, mark obs invalid for that sample
    for i, rs in enumerate(ref_states):
        if not rs.valid and i < len(obs_states):
            obs_states[i] = OrbitState(
                time=obs_states[i].time,
                r_km=obs_states[i].r_km,
                v_km_s=obs_states[i].v_km_s,
                norad_id=obs_states[i].norad_id,
                valid=False,
                error="ref_invalid",
            )

    cusum = ResidualCUSUM(cusum_config)
    result = detect_from_dual_source(
        ref_states,
        obs_states,
        norad_id=el.norad_id,
        name=el.name or ISS_NAME,
        cusum=cusum,
    )

    mags = [
        s.magnitude_km
        for s in result.residuals
        if s.magnitude_km == s.magnitude_km
    ]
    mean_mag = float(np.mean(mags)) if mags else float("nan")
    max_mag = float(np.max(mags)) if mags else float("nan")
    median_mag = float(np.median(mags)) if mags else float("nan")

    def _comp(attr: str) -> dict[str, float]:
        vals = [
            getattr(s, attr)
            for s in result.residuals
            if math.isfinite(getattr(s, attr))
        ]
        if not vals:
            return {"median_km": float("nan"), "rms_km": float("nan")}
        arr = np.array(vals, dtype=float)
        return {
            "median_km": float(np.median(arr)),
            "rms_km": float(np.sqrt(np.mean(arr * arr))),
        }

    rtn = {
        "radial": _comp("radial_km"),
        "along_track": _comp("along_track_km"),
        "cross_track": _comp("cross_track_km"),
    }
    # ISS LEO speed ~7.66 km/s: along-track km → equivalent timing error
    iss_km_s = 7.66
    t_med = rtn["along_track"]["median_km"]
    timing_s = (
        float(t_med / iss_km_s) if math.isfinite(t_med) else float("nan")
    )
    last = obs_states[-1] if obs_states else None
    now_obs = None
    if last and last.valid:
        lat, lon, alt = teme_to_latlon_alt(last)
        now_obs = {
            "t": last.time.isoformat(),
            "lat": lat,
            "lon": lon,
            "alt_km": alt,
        }
    events: list[dict] = []
    try:
        raw_text = Path(
            eph.meta.source_url
        ).read_text(encoding="utf-8") if eph.meta.source_mode == "fixture" else ""
        if eph.meta.source_mode in ("live", "cache"):
            from orbital.oem import _CACHE_PATH

            if _CACHE_PATH.is_file():
                raw_text = _CACHE_PATH.read_text(encoding="utf-8")
        if raw_text:
            events = parse_oem_events(raw_text)
    except OSError:
        events = []

    result.meta.update(
        {
            "observation_source": "nasa_iss_oem",
            "observation_mode": eph.meta.source_mode,
            "observation_url": eph.meta.source_url,
            "observation_frame": eph.meta.ref_frame or "EME2000",
            "observation_originator": eph.meta.originator,
            "observation_creation": eph.meta.creation_date,
            "reference_source": el.source,
            "reference_frame": "TEME (SGP4)",
            "reference_tle_epoch": el.epoch.isoformat(),
            "frame_disclosure": FRAME_DISCLOSURE,
            "residual_floor_note": RESIDUAL_FLOOR_NOTE_KM,
            "mean_magnitude_km": mean_mag,
            "median_magnitude_km": median_mag,
            "max_magnitude_km": max_mag,
            "rtn": rtn,
            "timing_error_s": timing_s,
            "iss_speed_km_s": iss_km_s,
            "now_obs": now_obs,
            "oem_events": events,
            "n_oem_raw": len(obs_raw),
            "k": cusum_config.k,
            "h": cusum_config.h,
            "pipeline": "oem_vs_sgp4_ecef",
            "synthetic": False,
            "time_span": {
                "start": times[0].isoformat() if times else None,
                "stop": times[-1].isoformat() if times else None,
            },
        }
    )
    return result


def observe_status() -> dict[str, Any]:
    """Lightweight status for health/UI (does not force live fetch)."""
    from orbital.oem import _CACHE_PATH, _FIXTURE_PATH, _cache_age_s

    return {
        "oem_url": "https://nasa-public-data.s3.amazonaws.com/iss-coords/current/ISS_OEM/ISS.OEM_J2K_EPH.txt",
        "fixture_present": _FIXTURE_PATH.is_file(),
        "cache_present": _CACHE_PATH.is_file(),
        "cache_age_seconds": _cache_age_s(),
        "frame_disclosure": FRAME_DISCLOSURE,
        "iss_norad_id": ISS_NORAD_ID,
    }
