"""Orbital domain models (Orbit Ghost). Do not import air StateVector here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class GpElement:
    """General perturbations element set (TLE/OMM-compatible fields)."""

    norad_id: int
    name: str
    epoch: datetime
    line1: str
    line2: str
    source: str = "fixture"  # fixture | celestrak | space_track
    object_id: str = ""  # international designator if known
    mean_motion: float = 0.0  # rev/day if known
    eccentricity: float = 0.0
    inclination_deg: float = 0.0


@dataclass
class OrbitState:
    """Cartesian state in TEME (SGP4 native), km and km/s."""

    time: datetime
    r_km: tuple[float, float, float]
    v_km_s: tuple[float, float, float]
    norad_id: int = 0
    valid: bool = True
    error: str = ""


@dataclass
class ResidualSample:
    """RTN residual at one sample time (observed − reference), km."""

    time: datetime
    radial_km: float
    along_track_km: float
    cross_track_km: float
    magnitude_km: float


@dataclass
class ManeuverFlag:
    """Detected residual anomaly for one object."""

    norad_id: int
    name: str
    time: datetime
    flag_type: str  # cusum_drift | residual_threshold | prop_error
    residual_magnitude_km: float
    cusum_score: float
    residual_rtn_km: tuple[float, float, float]
    severity: float = 0.0  # 0–1
    recommended_action: str = "reobserve"  # heuristic until LLM P1.5


@dataclass
class DetectionResult:
    """Pipeline output for one object / residual series."""

    norad_id: int
    name: str
    residuals: list[ResidualSample]
    flags: list[ManeuverFlag]
    flagged: bool
    meta: dict = field(default_factory=dict)

    @property
    def flag_count(self) -> int:
        return len(self.flags)


@dataclass
class EvalMetrics:
    """Binary detection metrics for synthetic suite."""

    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int
    n_clean: int
    n_anomalous: int
    separation_ratio: float = 0.0  # mean mag anomalous / mean mag clean
    dv_boundary_m_s: float = 0.0  # min Δv with ≥95% detection
    disclosure: str = (
        "Synthetic Δv suite: labels are known by construction. "
        "Not a claim on real on-orbit maneuver ground truth."
    )


@dataclass
class SourceStatus:
    """Backend health for mode banner."""

    mode: str  # FIXTURE | LIVE_CELESTRAK | DEGRADED
    backend: str
    detail: str = ""
    last_fetch_utc: Optional[datetime] = None
    cache_age_seconds: Optional[float] = None
