"""Data models for Ghost Track detection pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class StateVector:
    """A single ADS-B state vector (position + velocity report)."""

    icao24: str  # ICAO 24-bit aircraft address
    callsign: str  # Flight callsign (may be empty)
    time: datetime  # Measurement timestamp (UTC)
    latitude: float  # degrees
    longitude: float  # degrees
    altitude: float  # barometric altitude, meters (may be NaN if unavailable)
    velocity: float  # m/s (may be NaN)
    heading: float  # degrees (may be NaN)
    vertical_rate: float  # m/s (may be NaN)
    on_ground: bool = False
    region: str = ""
    source: str = "opensky"  # data source identifier
    # Extended OpenSky fields (optional)
    origin_country: str = ""
    last_contact: Optional[datetime] = None
    geo_altitude: float = float("nan")
    squawk: str = ""
    spi: bool = False
    position_source: int = 0
    category: int = 0


@dataclass
class AircraftTrack:
    """Time-ordered sequence of state vectors for a single aircraft."""

    icao24: str
    callsign: str
    states: list[StateVector] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        if len(self.states) < 2:
            return 0.0
        return (self.states[-1].time - self.states[0].time).total_seconds()

    @property
    def sample_count(self) -> int:
        return len(self.states)

    def sorted(self) -> "AircraftTrack":
        return AircraftTrack(
            icao24=self.icao24,
            callsign=self.callsign,
            states=sorted(self.states, key=lambda s: s.time),
        )


@dataclass
class FilterState:
    """Kalman filter state at a single timestep."""

    time: datetime
    x: list[float]  # state vector [lat, lon, alt, v_n, v_e, v_u]
    P: list[list[float]]  # covariance matrix (6x6, flattened row-major)
    innovation: float  # Mahalanobis distance of measurement residual
    residual_raw: list[float]  # raw residual [d_lat, d_lon, d_alt]
    cusum_positive: float = 0.0  # running CUSUM+ at this step
    cusum_negative: float = 0.0  # running CUSUM- at this step


@dataclass
class AnomalyFlag:
    """A single detected anomaly at a specific time/position."""

    icao24: str
    callsign: str
    time: datetime
    latitude: float
    longitude: float
    altitude: float
    flag_type: str  # "position_jump", "cusum_drift", "multi_source_mismatch"
    mahalanobis_distance: float
    cusum_score: float
    residual_components: list[float]  # [d_lat, d_lon, d_alt] raw residuals
    region: str = ""
    severity: float = 0.0  # 0–1 initial detector severity


@dataclass
class DetectionResult:
    """Full detection output for a single aircraft track."""

    track: AircraftTrack
    filter_states: list[FilterState]
    anomalies: list[AnomalyFlag]
    flagged: bool  # True if any anomaly detected

    @property
    def anomaly_count(self) -> int:
        return len(self.anomalies)


@dataclass
class TriageReport:
    """Output from the triage agent for one incident cluster."""

    incident_id: str
    aircraft_ids: list[str]
    time_start: datetime
    time_end: datetime
    region: str
    anomaly_count: int
    severity_score: int  # 1–5
    summary: str  # NL summary from LLM
    recommended_action: str  # NL recommendation
    cross_references: list[str]  # GPSJam context, other sources
    claims: list[str] = field(default_factory=list)  # verifiable claims for hallucination check


@dataclass
class EvaluationResult:
    """Precision/recall/hallucination metrics for one evaluation run."""

    # Detector metrics
    detector_precision: float
    detector_recall: float
    detector_f1: float
    true_positives: int
    false_positives: int
    false_negatives: int

    # Triage metrics
    triage_precision: float = 0.0
    triage_recall: float = 0.0
    triage_f1: float = 0.0

    # Hallucination
    hallucination_rate: float = 0.0  # fraction of summaries with >=1 unverifiable claim
    total_claims_checked: int = 0
    unverifiable_claims: int = 0

    # Adversarial
    drift_detection_boundary: float = 0.0  # min drift rate caught (units/sec)
    drift_detection_boundary_units: str = "m/s"
