"""Kalman filter position-conformance detector.

Implements a 6-state constant-velocity Kalman filter for ADS-B track
validation. Flags anomalies when the Mahalanobis distance between predicted
and reported position exceeds a configurable threshold (Test 1 from
Krozel et al. and the 2021 MDPI paper).

State vector: [latitude, longitude, altitude, v_north, v_east, v_up]
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from config import config
from detector.models import AircraftTrack, AnomalyFlag, DetectionResult, FilterState, StateVector

logger = logging.getLogger(__name__)

# Earth radius for coordinate conversion
R_EARTH = 6371000.0  # meters
DEG_TO_RAD = np.pi / 180.0


class KalmanDetector:
    """6-state constant-velocity Kalman filter for ADS-B position validation."""

    def __init__(
        self,
        mahalanobis_threshold: float = 3.0,
        process_noise_scale: float = 1.0,
        measurement_noise_position: float = 10.0,  # meters (GPS accuracy)
        measurement_noise_altitude: float = 15.0,  # meters (barometric accuracy)
    ):
        self.mahalanobis_threshold = mahalanobis_threshold
        self.process_noise_scale = process_noise_scale

        # State dimension: [lat(deg), lon(deg), alt(m), v_n(m/s), v_e(m/s), v_u(m/s)]
        self.dim_x = 6
        self.dim_z = 3  # We observe [lat, lon, alt]

        # Measurement noise (in measurement space)
        self.R = np.diag(
            [
                (measurement_noise_position / R_EARTH) * (180.0 / np.pi),
                (measurement_noise_position / (R_EARTH * np.cos(40 * DEG_TO_RAD)))
                * (180.0 / np.pi),
                measurement_noise_altitude,
            ]
        )

        # State transition matrix (constant velocity)
        self.F = np.eye(self.dim_x)
        # Will be updated with dt at each step

        # Measurement function (we observe position directly)
        self.H = np.zeros((self.dim_z, self.dim_x))
        self.H[0, 0] = 1.0  # lat
        self.H[1, 1] = 1.0  # lon
        self.H[2, 2] = 1.0  # alt

        # Initial state covariance
        self.P0 = np.diag(
            [
                0.0001,  # lat variance (deg^2)
                0.0001,  # lon variance (deg^2)
                100.0,  # alt variance (m^2)
                100.0,  # v_n variance (m/s)^2
                100.0,  # v_e variance (m/s)^2
                25.0,  # v_u variance (m/s)^2
            ]
        )

        # Per-track persistent state for incremental processing
        # icao24 -> {"x": ndarray, "P": ndarray, "last_time": datetime,
        #            "filter_states": list, "anomalies": list}
        self._track_states: dict[str, dict] = {}

    def reset_track(self, icao24: str) -> None:
        """Clear saved incremental state for a track (e.g. between eval trials)."""
        self._track_states.pop(icao24, None)

    def process_noise_covariance(self, dt: float) -> np.ndarray:
        """Discrete-time process noise covariance (piecewise white noise model).

        Q = ∫ Φ(t) Q_c Φ(t)^T dt where Q_c is the continuous process noise.
        Uses the common discretization for constant-velocity models.
        """
        q = self.process_noise_scale * 0.01  # process noise intensity (m/s^2)^2
        dt2 = dt * dt / 2.0
        dt3 = dt * dt * dt / 3.0

        Q = np.zeros((self.dim_x, self.dim_x))

        # Position-velocity cross terms for lat
        Q[0, 0] = dt3 * q
        Q[0, 3] = dt2 * q
        Q[3, 0] = dt2 * q
        Q[3, 3] = dt * q

        # Position-velocity cross terms for lon
        Q[1, 1] = dt3 * q
        Q[1, 4] = dt2 * q
        Q[4, 1] = dt2 * q
        Q[4, 4] = dt * q

        # Position-velocity cross terms for alt
        Q[2, 2] = dt3 * q
        Q[2, 5] = dt2 * q
        Q[5, 2] = dt2 * q
        Q[5, 5] = dt * q

        return Q

    def _state_transition(self, dt: float) -> np.ndarray:
        """Build state transition matrix for timestep dt."""
        F = np.eye(self.dim_x)
        F[0, 3] = dt  # lat += v_n * dt
        F[1, 4] = dt  # lon += v_e * dt
        F[2, 5] = dt  # alt += v_u * dt
        return F

    def _initialize_state(self, sv: StateVector) -> tuple[np.ndarray, np.ndarray]:
        """Initialize filter state from first measurement."""
        x = np.zeros(self.dim_x)
        x[0] = sv.latitude
        x[1] = sv.longitude
        x[2] = sv.altitude if not np.isnan(sv.altitude) else 0.0

        # Initialize velocity from reported values if available
        if not np.isnan(sv.velocity) and not np.isnan(sv.heading):
            heading_rad = sv.heading * DEG_TO_RAD
            x[3] = sv.velocity * np.cos(heading_rad)  # v_n
            x[4] = sv.velocity * np.sin(heading_rad)  # v_e
        if not np.isnan(sv.vertical_rate):
            x[5] = sv.vertical_rate  # v_u

        return x, self.P0.copy()

    def _measurement_from_state(self, sv: StateVector) -> np.ndarray:
        """Extract measurement vector from state vector."""
        z = np.zeros(self.dim_z)
        z[0] = sv.latitude
        z[1] = sv.longitude
        alt = sv.altitude
        z[2] = alt if not np.isnan(alt) else 0.0
        return z

    def _mahalanobis(self, innovation: np.ndarray, S: np.ndarray) -> float:
        """Compute Mahalanobis distance: sqrt(y^T S^{-1} y)."""
        try:
            return float(np.sqrt(innovation.T @ np.linalg.solve(S, innovation)))
        except np.linalg.LinAlgError:
            return float("inf")

    def process_track(self, track: AircraftTrack) -> DetectionResult:
        """Run Kalman filter over an aircraft track, flagging anomalies.

        Incremental: saves filter state per aircraft and only processes
        samples newer than the last-processed timestamp on subsequent calls.
        """
        states = track.sorted().states
        if len(states) < 2:
            return DetectionResult(track=track, filter_states=[], anomalies=[], flagged=False)

        icao24 = track.icao24
        saved = self._track_states.get(icao24)

        # Determine start index and initial state
        if saved is not None:
            x = saved["x"].copy()
            P = saved["P"].copy()
            last_time = saved["last_time"]

            # Find first state newer than last processed time
            start_idx = len(states)
            for i, sv in enumerate(states):
                if sv.time > last_time:
                    start_idx = i
                    break

            if start_idx >= len(states):
                # No new states — do not re-emit historical anomalies
                return DetectionResult(
                    track=track,
                    filter_states=list(saved["filter_states"]),
                    anomalies=[],
                    flagged=len(saved["anomalies"]) > 0,
                )

            # If the first new sample is the first sample overall, the track
            # was likely replaced — fall through to full reinitialization
            if start_idx == 0:
                saved = None
            else:
                prev_time = last_time

        if saved is None:
            x, P = self._initialize_state(states[0])
            last_time = states[0].time
            start_idx = 0
            prev_time = last_time
            self._track_states[icao24] = {
                "x": x, "P": P, "last_time": last_time,
                "filter_states": [], "anomalies": [],
            }

        # Process only new samples
        new_fs: list[FilterState] = []
        new_anomalies: list[AnomalyFlag] = []

        for i in range(start_idx, len(states)):
            sv = states[i]
            dt = (sv.time - prev_time).total_seconds()
            if dt <= 0:
                dt = 1.0
            prev_time = sv.time

            # --- Predict ---
            F = self._state_transition(dt)
            Q = self.process_noise_covariance(dt)
            x = F @ x
            P = F @ P @ F.T + Q

            # --- Update ---
            z = self._measurement_from_state(sv)
            y = z - self.H @ x
            S = self.H @ P @ self.H.T + self.R
            mahal = self._mahalanobis(y, S)

            K = P @ self.H.T @ np.linalg.inv(S)
            x = x + K @ y
            P = (np.eye(self.dim_x) - K @ self.H) @ P

            fs = FilterState(
                time=sv.time,
                x=x.tolist(),
                P=P.flatten().tolist(),
                innovation=mahal,
                residual_raw=y.tolist(),
            )
            new_fs.append(fs)

            if mahal > self.mahalanobis_threshold:
                region = _classify_region(sv.latitude, sv.longitude, config)
                severity = min(1.0, mahal / (self.mahalanobis_threshold * 3))
                new_anomalies.append(
                    AnomalyFlag(
                        icao24=sv.icao24,
                        callsign=sv.callsign,
                        time=sv.time,
                        latitude=sv.latitude,
                        longitude=sv.longitude,
                        altitude=sv.altitude,
                        flag_type="position_jump",
                        mahalanobis_distance=mahal,
                        cusum_score=0.0,
                        residual_components=y.tolist(),
                        region=region,
                        severity=severity,
                    )
                )

        # Persist updated state (cap history for memory)
        entry = self._track_states[icao24]
        entry["x"] = x
        entry["P"] = P
        entry["last_time"] = states[-1].time
        entry["filter_states"].extend(new_fs)
        entry["anomalies"].extend(new_anomalies)
        max_hist = 120
        if len(entry["filter_states"]) > max_hist:
            entry["filter_states"] = entry["filter_states"][-max_hist:]
        if len(entry["anomalies"]) > max_hist:
            entry["anomalies"] = entry["anomalies"][-max_hist:]

        # Emit only THIS cycle's anomalies (prevents store re-ingestion)
        return DetectionResult(
            track=track,
            filter_states=list(entry["filter_states"]),
            anomalies=list(new_anomalies),
            flagged=len(entry["anomalies"]) > 0 or len(new_anomalies) > 0,
        )


def _classify_region(lat: float, lon: float, cfg) -> str:
    for name, (mn_lat, mx_lat, mn_lon, mx_lon) in cfg.regions.items():
        if mn_lat <= lat <= mx_lat and mn_lon <= lon <= mx_lon:
            return name
    return "unknown"
