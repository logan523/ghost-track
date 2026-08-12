"""FastAPI router for Orbit Ghost (/orbital/*)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from orbital.cusum import CUSUMConfig
from orbital.detect import detect_synthetic
from orbital.eval.adversarial import run_synthetic_suite
from orbital.ingest import get_backend
from orbital.models import EvalMetrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orbital", tags=["orbital"])

_latest_eval: Optional[dict[str, Any]] = None


class DetectRequest(BaseModel):
    dv_m_s: float = Field(0.0, description="Synthetic Δv (m/s); 0 = clean")
    n_samples: int = Field(120, ge=10, le=2000)
    k: float = Field(0.05, description="CUSUM reference (km)")
    h: float = Field(0.5, description="CUSUM threshold (km)")


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
def health() -> dict[str, Any]:
    backend = get_backend()
    st = backend.status()
    return {
        "service": "orbit-ghost",
        "mode": st.mode,
        "backend": st.backend,
        "detail": st.detail,
        "cache_age_seconds": st.cache_age_seconds,
        "last_fetch_utc": st.last_fetch_utc.isoformat() if st.last_fetch_utc else None,
    }


@router.get("/catalog")
def catalog() -> dict[str, Any]:
    backend = get_backend()
    try:
        els = backend.list_elements()
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    st = backend.status()
    return {
        "mode": st.mode,
        "count": len(els),
        "objects": [
            {
                "norad_id": e.norad_id,
                "name": e.name,
                "source": e.source,
                "inclination_deg": e.inclination_deg,
                "mean_motion": e.mean_motion,
            }
            for e in els[:200]
        ],
    }


@router.post("/detect")
def detect(req: DetectRequest) -> dict[str, Any]:
    cfg = CUSUMConfig(k=req.k, h=req.h)
    result = detect_synthetic(
        dv_m_s=req.dv_m_s, n=req.n_samples, cusum_config=cfg
    )
    trail = result.meta.get("trail") or []
    meta = {k: v for k, v in result.meta.items() if k != "trail"}
    return {
        "norad_id": result.norad_id,
        "name": result.name,
        "flagged": result.flagged,
        "flag_count": result.flag_count,
        "meta": meta,
        "trail": trail,
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
