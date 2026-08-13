"""FastAPI router for Orbit Ghost (/orbital/*)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from orbital.cusum import CUSUMConfig
from orbital.detect import detect_synthetic, field_scan
from orbital.eval.adversarial import run_synthetic_suite
from orbital.frames import FRAME_DISCLOSURE
from orbital.ingest import (
    ALLOWED_GROUPS,
    DEFAULT_GROUP,
    custody_age_hours,
    custody_tier,
    get_backend,
    max_objects_cap,
)
from orbital.models import EvalMetrics, GpElement
from orbital.observe import observe_iss_residual, observe_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orbital", tags=["orbital"])

_latest_eval: Optional[dict[str, Any]] = None
_latest_observe: Optional[dict[str, Any]] = None


class DetectRequest(BaseModel):
    dv_m_s: float = Field(0.0, description="Synthetic Δv (m/s); 0 = clean")
    n_samples: int = Field(120, ge=10, le=2000)
    k: float = Field(0.05, description="CUSUM reference (km)")
    h: float = Field(0.5, description="CUSUM threshold (km)")
    norad_id: Optional[int] = Field(
        None, description="Catalog object; default = first catalog element"
    )
    group: str = Field(DEFAULT_GROUP, description="Catalog group for lookup")


class ObserveIssRequest(BaseModel):
    """NASA ISS OEM (observed) vs SGP4 TLE (reference) — real dual-source residual."""

    n_samples: int = Field(
        90,
        ge=10,
        le=500,
        description="Max OEM states (evenly subsampled)",
    )
    oem_source: str = Field(
        "auto",
        description="auto | live | fixture | cache",
    )
    k: float = Field(
        1.5,
        description="CUSUM reference on excess residual (km above baseline floor)",
    )
    h: float = Field(
        12.0,
        description="CUSUM threshold on accumulated excess (km)",
    )
    norad_id: Optional[int] = Field(
        25544,
        description="Reference TLE NORAD id (default ISS 25544)",
    )
    group: str = Field(DEFAULT_GROUP, description="Catalog group for TLE lookup")


class FieldScanRequest(BaseModel):
    group: str = Field(DEFAULT_GROUP)
    limit: Optional[int] = Field(None, ge=1, le=2000)
    orbit_samples: int = Field(48, ge=8, le=128)
    include_orbits: bool = True


def _resolve_element(
    norad_id: Optional[int], group: str = DEFAULT_GROUP
) -> Optional[GpElement]:
    if norad_id is None:
        return None
    backend = get_backend()
    for el in backend.list_elements(group=group):
        if el.norad_id == norad_id:
            return el
    raise HTTPException(
        status_code=404,
        detail=f"NORAD {norad_id} not in catalog group={group} ({backend.status().backend})",
    )


def _finite(v: float) -> Optional[float]:
    return float(v) if v == v else None


def _chart_series(result, max_points: int = 160) -> list[dict[str, Any]]:
    residuals = result.residuals
    cusum_pos = result.meta.get("cusum_pos") or []
    n = len(residuals)
    if n == 0:
        return []
    stride = max(1, n // max_points)
    chart: list[dict[str, Any]] = []
    for i in range(0, n, stride):
        s = residuals[i]
        chart.append(
            {
                "t": s.time.isoformat(),
                "mag_km": _finite(s.magnitude_km),
                "rtn_km": [
                    _finite(s.radial_km),
                    _finite(s.along_track_km),
                    _finite(s.cross_track_km),
                ],
                "cusum": float(cusum_pos[i]) if i < len(cusum_pos) else 0.0,
            }
        )
    return chart


def _catalog_row(e: GpElement) -> dict[str, Any]:
    age = custody_age_hours(e.epoch)
    return {
        "norad_id": e.norad_id,
        "name": e.name,
        "source": e.source,
        "inclination_deg": e.inclination_deg,
        "mean_motion": e.mean_motion,
        "epoch": e.epoch.isoformat(),
        "custody_age_hours": round(age, 2),
        "custody_tier": custody_tier(age),
    }


class EvalRequest(BaseModel):
    n_clean: int = Field(15, ge=1, le=100)
    n_anomalous: int = Field(15, ge=1, le=100)
    dv_m_s: float = Field(2.0, gt=0)
    n_samples: int = Field(120, ge=20, le=500)
    seed: int = 42


def _metrics_to_dict(m: EvalMetrics) -> dict[str, Any]:
    return {
        "precision": m.precision,
        "recall": m.recall,
        "f1": m.f1,
        "true_positives": m.true_positives,
        "false_positives": m.false_positives,
        "false_negatives": m.false_negatives,
        "n_clean": m.n_clean,
        "n_anomalous": m.n_anomalous,
        "separation_ratio": m.separation_ratio,
        "dv_boundary_m_s": m.dv_boundary_m_s,
        "disclosure": m.disclosure,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health")
def health(
    group: str = Query(DEFAULT_GROUP),
) -> dict[str, Any]:
    backend = get_backend()
    st = backend.status()
    try:
        els = backend.list_elements(group=group)
        count = len(els)
    except Exception:
        count = 0
    obs = observe_status()
    return {
        "service": "orbit-ghost",
        "mode": st.mode,
        "backend": st.backend,
        "detail": st.detail,
        "group": group if group in ALLOWED_GROUPS else DEFAULT_GROUP,
        "catalog_count": count,
        "max_objects": max_objects_cap(group),
        "cache_age_seconds": st.cache_age_seconds,
        "last_fetch_utc": st.last_fetch_utc.isoformat() if st.last_fetch_utc else None,
        "observe": {
            "oem_fixture": obs["fixture_present"],
            "oem_cache": obs["cache_present"],
            "oem_cache_age_seconds": obs["cache_age_seconds"],
            "iss_norad_id": obs["iss_norad_id"],
        },
    }


@router.get("/observe/status")
def observe_status_route() -> dict[str, Any]:
    return observe_status()


@router.post("/observe/iss")
def observe_iss(req: ObserveIssRequest) -> dict[str, Any]:
    """Real NASA ISS OEM vs public TLE/SGP4 residual + CUSUM.

    Not synthetic Δv. Residual includes disclosed model/frame floor.
    """
    global _latest_observe
    src = (req.oem_source or "auto").strip().lower()
    if src not in ("auto", "live", "fixture", "cache"):
        raise HTTPException(
            status_code=400,
            detail="oem_source must be auto|live|fixture|cache",
        )
    el = None
    if req.norad_id is not None:
        try:
            el = _resolve_element(req.norad_id, group=req.group)
        except HTTPException:
            # Fall back to ISS fixture element if catalog miss
            el = None
    try:
        result = observe_iss_residual(
            n_samples=req.n_samples,
            oem_source=src,
            k=req.k,
            h=req.h,
            el=el,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        logger.exception("observe/iss failed")
        raise HTTPException(status_code=502, detail=f"observe failed: {e}") from e

    trail = result.meta.get("trail") or []
    chart = _chart_series(result)
    meta = {
        k: v for k, v in result.meta.items() if k not in ("trail", "cusum_pos")
    }
    age = None
    if el is not None:
        age = custody_age_hours(el.epoch)
    elif result.meta.get("reference_tle_epoch"):
        try:
            ep = datetime.fromisoformat(
                str(result.meta["reference_tle_epoch"]).replace("Z", "+00:00")
            )
            age = custody_age_hours(ep)
        except ValueError:
            pass

    payload = {
        "norad_id": result.norad_id,
        "name": result.name,
        "flagged": result.flagged,
        "flag_count": result.flag_count,
        "k": req.k,
        "h": req.h,
        "mode": "NASA_OEM_VS_SGP4",
        "synthetic": False,
        "custody_age_hours": round(age, 2) if age is not None else None,
        "custody_tier": custody_tier(age) if age is not None else None,
        "meta": meta,
        "frame_disclosure": FRAME_DISCLOSURE,
        "trail": trail,
        "chart": chart,
        "flags": [
            {
                "time": f.time.isoformat(),
                "flag_type": f.flag_type,
                "residual_magnitude_km": f.residual_magnitude_km,
                "cusum_score": f.cusum_score,
                "severity": f.severity,
                "recommended_action": f.recommended_action,
                "residual_rtn_km": list(f.residual_rtn_km),
            }
            for f in result.flags
        ],
        "residual_summary": {
            "n": len(result.residuals),
            "max_magnitude_km": max(
                (
                    s.magnitude_km
                    for s in result.residuals
                    if s.magnitude_km == s.magnitude_km
                ),
                default=0.0,
            ),
            "mean_magnitude_km": meta.get("mean_magnitude_km"),
            "median_magnitude_km": meta.get("median_magnitude_km"),
            "rtn": meta.get("rtn"),
            "timing_error_s": meta.get("timing_error_s"),
            "iss_speed_km_s": meta.get("iss_speed_km_s"),
        },
        "sources": {
            "observed": {
                "name": "NASA/JSC TOPO ISS OEM",
                "operator": meta.get("observation_originator"),
                "url": meta.get("observation_url"),
                "frame": meta.get("observation_frame"),
                "mode": meta.get("observation_mode"),
                "created": meta.get("observation_creation"),
                "n_states": meta.get("n_oem_raw"),
                "window": meta.get("time_span"),
            },
            "reference": {
                "name": "CelesTrak TLE → SGP4",
                "source": meta.get("reference_source"),
                "frame": meta.get("reference_frame"),
                "tle_epoch": meta.get("reference_tle_epoch"),
            },
        },
        "now_obs": meta.get("now_obs"),
        "oem_events": meta.get("oem_events") or [],
    }
    _latest_observe = payload
    return payload


@router.get("/observe/iss/events")
def observe_iss_events(
    oem_source: str = Query("auto"),
) -> dict[str, Any]:
    """OEM COMMENT maneuver table (empty when TOPO published none)."""
    from orbital.oem import get_iss_oem, parse_oem_events, _CACHE_PATH, _FIXTURE_PATH

    src = (oem_source or "auto").strip().lower()
    if src not in ("auto", "live", "fixture", "cache"):
        raise HTTPException(status_code=400, detail="oem_source must be auto|live|fixture|cache")
    eph = get_iss_oem(source=src, max_states=1)
    text = ""
    try:
        if eph.meta.source_mode == "fixture":
            text = _FIXTURE_PATH.read_text(encoding="utf-8")
        elif _CACHE_PATH.is_file():
            text = _CACHE_PATH.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=503, detail=f"OEM text unavailable: {e}") from e
    events = parse_oem_events(text) if text else []
    return {
        "source_mode": eph.meta.source_mode,
        "originator": eph.meta.originator,
        "count": len(events),
        "events": events,
    }


@router.get("/observe/latest")
def observe_latest() -> dict[str, Any]:
    if _latest_observe is None:
        raise HTTPException(
            status_code=404,
            detail="No observe run yet; POST /orbital/observe/iss first",
        )
    return _latest_observe


@router.get("/groups")
def groups() -> dict[str, Any]:
    return {
        "groups": [
            {"id": g, "max_objects": max_objects_cap(g)} for g in ALLOWED_GROUPS
        ],
        "default": DEFAULT_GROUP,
    }


@router.get("/catalog")
def catalog(
    group: str = Query(DEFAULT_GROUP),
    limit: Optional[int] = Query(None, ge=1, le=2000),
) -> dict[str, Any]:
    backend = get_backend()
    try:
        els = backend.list_elements(group=group, limit=limit)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    st = backend.status()
    return {
        "mode": st.mode,
        "backend": st.backend,
        "group": group if group in ALLOWED_GROUPS else DEFAULT_GROUP,
        "count": len(els),
        "max_objects": max_objects_cap(group),
        "objects": [_catalog_row(e) for e in els],
    }


@router.post("/field/scan")
def field_scan_route(req: FieldScanRequest) -> dict[str, Any]:
    backend = get_backend()
    try:
        els = backend.list_elements(group=req.group, limit=req.limit)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    st = backend.status()
    payload = field_scan(
        els,
        orbit_samples=req.orbit_samples,
        include_orbits=req.include_orbits,
    )
    payload["mode"] = st.mode
    payload["backend"] = st.backend
    payload["group"] = req.group if req.group in ALLOWED_GROUPS else DEFAULT_GROUP
    payload["cache_age_seconds"] = st.cache_age_seconds
    return payload


@router.post("/detect")
def detect(req: DetectRequest) -> dict[str, Any]:
    cfg = CUSUMConfig(k=req.k, h=req.h)
    el = _resolve_element(req.norad_id, group=req.group)
    result = detect_synthetic(
        dv_m_s=req.dv_m_s, n=req.n_samples, cusum_config=cfg, el=el
    )
    trail = result.meta.get("trail") or []
    chart = _chart_series(result)
    meta = {
        k: v for k, v in result.meta.items() if k not in ("trail", "cusum_pos")
    }
    age = custody_age_hours(el.epoch) if el else None
    return {
        "norad_id": result.norad_id,
        "name": result.name,
        "flagged": result.flagged,
        "flag_count": result.flag_count,
        "k": req.k,
        "h": req.h,
        "dv_m_s": req.dv_m_s,
        "custody_age_hours": round(age, 2) if age is not None else None,
        "custody_tier": custody_tier(age) if age is not None else None,
        "meta": meta,
        "trail": trail,
        "chart": chart,
        "flags": [
            {
                "time": f.time.isoformat(),
                "flag_type": f.flag_type,
                "residual_magnitude_km": f.residual_magnitude_km,
                "cusum_score": f.cusum_score,
                "severity": f.severity,
                "recommended_action": f.recommended_action,
                "residual_rtn_km": list(f.residual_rtn_km),
            }
            for f in result.flags
        ],
        "residual_summary": {
            "n": len(result.residuals),
            "max_magnitude_km": max(
                (s.magnitude_km for s in result.residuals if s.magnitude_km == s.magnitude_km),
                default=0.0,
            ),
        },
    }


@router.post("/eval/run")
def eval_run(req: EvalRequest) -> dict[str, Any]:
    global _latest_eval
    metrics = run_synthetic_suite(
        n_clean=req.n_clean,
        n_anomalous=req.n_anomalous,
        dv_m_s=req.dv_m_s,
        n_samples=req.n_samples,
        seed=req.seed,
    )
    payload = _metrics_to_dict(metrics)
    _latest_eval = payload
    return payload


@router.get("/eval/latest")
def eval_latest() -> dict[str, Any]:
    if _latest_eval is None:
        raise HTTPException(
            status_code=404,
            detail="No eval run yet; POST /orbital/eval/run first",
        )
    return _latest_eval
