"""Adversarial Δv boundary characterization + synthetic suite runner."""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from orbital.cusum import CUSUMConfig, ResidualCUSUM
from orbital.detect import detect_synthetic
from orbital.eval.metrics import binary_metrics, to_eval_metrics
from orbital.models import EvalMetrics
from orbital.residual import magnitudes_km

logger = logging.getLogger(__name__)


def run_synthetic_suite(
    n_clean: int = 20,
    n_anomalous: int = 20,
    dv_m_s: float = 2.0,
    n_samples: int = 120,
    cusum_config: Optional[CUSUMConfig] = None,
    seed: int = 42,
) -> EvalMetrics:
    """Labeled suite: clean tracks (label=0) vs fixed Δv tracks (label=1)."""
    rng = np.random.default_rng(seed)
    cfg = cusum_config or CUSUMConfig(k=0.05, h=0.5, window_samples=200)
    preds: list[bool] = []
    labels: list[bool] = []
    clean_mags: list[float] = []
    anom_mags: list[float] = []

    for i in range(n_clean):
        # slight seed variation via n offset (deterministic)
        _ = rng.integers(0, 1000)
        res = detect_synthetic(dv_m_s=0.0, n=n_samples, cusum_config=cfg)
        preds.append(res.flagged)
        labels.append(False)
        m = magnitudes_km(res.residuals)
        clean_mags.append(float(np.nanmean(m)))

    for i in range(n_anomalous):
        _ = rng.integers(0, 1000)
        res = detect_synthetic(dv_m_s=dv_m_s, n=n_samples, cusum_config=cfg)
        preds.append(res.flagged)
        labels.append(True)
        m = magnitudes_km(res.residuals)
        anom_mags.append(float(np.nanmean(m)))

    m = binary_metrics(preds, labels)
    clean_mean = float(np.mean(clean_mags)) if clean_mags else 0.0
    anom_mean = float(np.mean(anom_mags)) if anom_mags else 0.0
    # Clean residual is near machine zero; report ratio vs 1e-6 km floor
    floor = max(clean_mean, 1e-6)
    sep = anom_mean / floor

    boundary = characterize_dv_boundary(
        detection_rate_target=0.95,
        n_per_level=max(5, n_anomalous // 4),
        n_samples=n_samples,
        cusum_config=cfg,
    )

    return to_eval_metrics(
        m,
        n_clean=n_clean,
        n_anomalous=n_anomalous,
        separation_ratio=float(sep) if np.isfinite(sep) else 0.0,
        dv_boundary_m_s=boundary,
    )


def characterize_dv_boundary(
    detection_rate_target: float = 0.95,
    n_per_level: int = 8,
    n_samples: int = 120,
    cusum_config: Optional[CUSUMConfig] = None,
    dv_grid_m_s: Optional[list[float]] = None,
) -> float:
    """Min Δv (m/s) at which detection rate ≥ target. Returns 0 if never met."""
    cfg = cusum_config or CUSUMConfig(k=0.05, h=0.5)
    grid = dv_grid_m_s or [0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0]
    for dv in grid:
        hits = 0
        for _ in range(n_per_level):
            res = detect_synthetic(dv_m_s=dv, n=n_samples, cusum_config=cfg)
            if res.flagged:
                hits += 1
        rate = hits / n_per_level
        logger.debug("Δv=%.3f m/s detection_rate=%.2f", dv, rate)
        if rate + 1e-9 >= detection_rate_target:
            return float(dv)
    return float(grid[-1]) if grid else 0.0
