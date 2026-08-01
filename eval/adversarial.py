"""Adversarial drift-injection test harness.

Injects synthetic slow position bias into real (or synthetic) historical
trajectories at controlled drift rates. Empirically characterizes the
detection boundary: the minimum drift rate at which the CUSUM/SPRT defense
catches the injected drift.

This quantified, honest limitation is a stronger portfolio artifact than
an unquantified robustness claim (§6 of the spec).
"""

import logging
import math
from copy import deepcopy
from datetime import timedelta
from typing import Optional

import numpy as np

from detector.cusum import CUSUMDetector
from detector.kalman import KalmanDetector
from detector.models import AircraftTrack, DetectionResult, StateVector

logger = logging.getLogger(__name__)


class AdversarialHarness:
    """Synthetic drift-injection test framework.

    Takes clean aircraft trajectories and injects controlled position bias
    to find the empirical detection boundary.
    """

    def __init__(
        self,
        kalman: Optional[KalmanDetector] = None,
        cusum: Optional[CUSUMDetector] = None,
    ):
        self.kalman = kalman or KalmanDetector()
        self.cusum = cusum or CUSUMDetector()

    def inject_position_drift(
        self,
        track: AircraftTrack,
        drift_rate_m_s: float,  # meters per second of position drift
        drift_start_fraction: float = 0.5,  # start drift at 50% of track
        drift_direction_deg: float = 0.0,  # direction of drift (0=North, 90=East)
    ) -> AircraftTrack:
        """Inject a constant-rate position drift into a copy of the track.

        The drift is applied in the local ENU frame: a constant velocity
        offset added to the reported position starting at the specified
        fraction of the track duration.

        Args:
            track: Clean aircraft track
            drift_rate_m_s: Drift rate in meters/second
            drift_start_fraction: When to start the drift (0–1 fraction of duration)
            drift_direction_deg: Direction of drift (0=N, 90=E, 180=S, 270=W)
        """
        modified = deepcopy(track)
        states = modified.sorted().states
        if len(states) < 2:
            return modified

        start_idx = int(len(states) * drift_start_fraction)

        # Convert drift rate from m/s to deg/s (approximate at mid-latitude)
        lat0 = states[0].latitude
        deg_per_m_lat = 1.0 / 111320.0
        deg_per_m_lon = 1.0 / (111320.0 * math.cos(math.radians(lat0)))

        drift_dir_rad = math.radians(drift_direction_deg)
        drift_lat_s = drift_rate_m_s * math.cos(drift_dir_rad) * deg_per_m_lat
        drift_lon_s = drift_rate_m_s * math.sin(drift_dir_rad) * deg_per_m_lon

        base_time = states[start_idx].time if start_idx < len(states) else None

        for i, sv in enumerate(states):
            if i >= start_idx and base_time is not None:
                elapsed = (sv.time - base_time).total_seconds()
                sv.latitude += drift_lat_s * elapsed
                sv.longitude += drift_lon_s * elapsed

        return modified

    def characterize_boundary(
        self,
        clean_tracks: list[AircraftTrack],
        drift_rates: Optional[list[float]] = None,
        num_trials_per_rate: int = 5,
    ) -> dict:
        """Empirically find the detection boundary across drift rates.

        For each drift rate, inject into clean tracks and measure what
        fraction are detected. Returns the full characterization curve.

        Args:
            clean_tracks: Tracks known to be clean (no anomalies)
            drift_rates: List of drift rates to test in m/s.
                         Defaults to a geometric progression from 0.01 to 100 m/s.
            num_trials_per_rate: Number of trials per drift rate (with different
                                directions/start times for variance)

        Returns:
            Dict with:
            - drift_rates: tested rates (m/s)
            - detection_rates: fraction detected at each rate
            - boundary_m_s: minimum rate with >=95% detection
            - per_rate_details: full detection results
        """
        if drift_rates is None:
            # Geometric progression: 0.01 to 100 m/s
            drift_rates = [0.01 * (2**i) for i in range(15)]
            drift_rates = [r for r in drift_rates if r <= 100.0]

        detection_rates = []
        per_rate_details = []

        for rate in drift_rates:
            detections = 0
            total = 0

            for track in clean_tracks:
                for trial in range(num_trials_per_rate):
                    # Vary direction and start fraction across trials
                    direction = (trial * 360 / num_trials_per_rate) % 360
                    start_frac = 0.3 + (trial * 0.1)

                    injected = self.inject_position_drift(
                        track,
                        drift_rate_m_s=rate,
                        drift_direction_deg=direction,
                        drift_start_fraction=start_frac,
                    )

                    kalman_result = self.kalman.process_track(injected)
                    cusum_result = self.cusum.process_result(kalman_result)

                    # Check if any CUSUM flags were raised
                    cusum_flags = [
                        a
                        for a in cusum_result.anomalies
                        if a.flag_type == "cusum_drift"
                    ]
                    if len(cusum_flags) > 0:
                        detections += 1
                    total += 1

            detection_rate = detections / total if total > 0 else 0.0
            detection_rates.append(detection_rate)
            per_rate_details.append(
                {
                    "drift_rate_m_s": rate,
                    "detections": detections,
                    "trials": total,
                    "detection_rate": detection_rate,
                }
            )
            logger.info(
                f"Drift rate {rate:.4f} m/s: {detections}/{total} detected "
                f"({detection_rate:.1%})"
            )

        # Find boundary: minimum rate with >=95% detection
        boundary = float("inf")
        for rate, dr in zip(drift_rates, detection_rates):
            if dr >= 0.95:
                boundary = rate
                break

        return {
            "drift_rates_m_s": drift_rates,
            "detection_rates": detection_rates,
            "boundary_m_s": boundary,
            "boundary_units": "m/s",
            "per_rate_details": per_rate_details,
            "summary": (
                f"CUSUM catches synthetic position drift ≥ {boundary:.4f} m/s "
                f"at ≥95% detection rate. Drift below this rate may evade "
                f"detection without a second independent data source."
            ),
        }

    def plot_characterization(self, result: dict, save_path: Optional[str] = None):
        """Generate a detection-probability-vs-drift-rate plot."""
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(8, 5))
        ax.semilogx(result["drift_rates_m_s"], result["detection_rates"], "b-o", markersize=4)
        ax.axvline(
            result["boundary_m_s"],
            color="r",
            linestyle="--",
            label=f"95% boundary: {result['boundary_m_s']:.4f} m/s",
        )
        ax.axhline(0.95, color="gray", linestyle=":", alpha=0.5)
        ax.set_xlabel("Drift rate (m/s)")
        ax.set_ylabel("Detection rate")
        ax.set_title("CUSUM Detection vs. Adversarial Position Drift")
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.tight_layout()

        if save_path:
            fig.savefig(save_path, dpi=150)
            logger.info(f"Saved drift characterization plot to {save_path}")

        return fig


def generate_clean_synthetic_track(
    duration_seconds: float = 600.0,
    sample_interval: float = 5.0,
) -> AircraftTrack:
    """Generate a clean synthetic track for adversarial testing."""
    from datetime import datetime, timezone

    lat0, lon0, alt0 = 50.0, 10.0, 10000.0  # Baltic Sea cruise
    heading, speed = 90.0, 250.0  # eastbound, m/s

    heading_rad = math.radians(heading)
    lat_rate = speed * math.cos(heading_rad) / 111320.0
    lon_rate = speed * math.sin(heading_rad) / (111320.0 * math.cos(math.radians(lat0)))

    n = int(duration_seconds / sample_interval)
    t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    states = []
    for i in range(n):
        dt = i * sample_interval
        lat = lat0 + lat_rate * dt
        lon = lon0 + lon_rate * dt
        alt = alt0 + np.random.normal(0, 5)

        states.append(
            StateVector(
                icao24="SYNTH01",
                callsign="TEST01",
                time=t0 + timedelta(seconds=dt),
                latitude=lat + np.random.normal(0, 0.0001),
                longitude=lon + np.random.normal(0, 0.0001),
                altitude=alt,
                velocity=speed + np.random.normal(0, 3),
                heading=heading + np.random.normal(0, 0.5),
                vertical_rate=np.random.normal(0, 0.3),
            )
        )

    return AircraftTrack(icao24="SYNTH01", callsign="TEST01", states=states)
