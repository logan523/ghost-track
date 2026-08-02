"""Unified Ghost Score and severity helpers."""

from __future__ import annotations


def compute_ghost_score(
    mahalanobis_distance: float = 0.0,
    cusum_score: float = 0.0,
    same_aircraft_flags: int = 0,
    nearby_region_flags: int = 0,
) -> int:
    """Single Ghost Score definition (0–100). Used by server and reports."""
    mahal_score = min(40, max(0, (mahalanobis_distance / 3.0) * 10))
    cusum_component = min(30, max(0, (cusum_score / 150.0) * 10))
    persistence_score = min(20, same_aircraft_flags * 3)
    density_score = min(10, nearby_region_flags)
    return int(round(mahal_score + cusum_component + persistence_score + density_score))


def report_ghost_score(anomaly_scores: list[int]) -> int:
    """Aggregate Ghost Score for a multi-anomaly incident."""
    if not anomaly_scores:
        return 0
    return int(round(max(anomaly_scores)))


def calibrate_severity_1_to_5(raw_severity: float, region_modifier: float) -> tuple[int, int]:
    """Apply region modifier to a 1–5 severity.

    region_modifier is typically 0.7–1.3 from triage.context severity_modifier.
    Returns (severity_raw, severity_score) both clamped 1–5.
    """
    raw = max(1, min(5, int(round(raw_severity))))
    # Map 0–1 style modifiers (~0.7–1.3) onto 1–5 scale
    calibrated = max(1, min(5, int(round(raw * region_modifier))))
    return raw, calibrated
