"""CUSUM (Cumulative Sum) drift detector for ADS-B filter residuals.

Implements two one-sided CUSUM tests on standardized Kalman filter innovations
to detect slow-drift GNSS spoofing attacks that evade per-sample thresholding.

Reference: Page (1954), "Continuous inspection schemes."
The CUSUM/SPRT approach reduces the window in which slow-drift attacks succeed;
it does NOT eliminate the attack class — an attacker can always drift slower
than the window, and widening the window increases false positives on legitimate
slow maneuvers (holding patterns, gradual descents).

Terminology: CUSUM / cumulative sum test / SPRT (sequential probability ratio
test) on filter innovations. Do NOT use the term "frog boiling attack" — that
comes from unrelated P2P network-coordinate-systems literature.
"""

import logging
from datetime import datetime
from typing import Optional

import numpy as np

from config import config
from detector.models import AircraftTrack, AnomalyFlag, DetectionResult, FilterState

logger = logging.getLogger(__name__)


class CUSUMDetector:
    """Windowed two-sided CUSUM on standardized Kalman filter residuals.

    Maintains two cumulative sums (positive and negative drift) and fires
    when either exceeds the decision threshold h. Uses a sliding window
    to allow reset after prolonged quiet periods and to avoid unbounded
    accumulation from legitimate slow maneuvers.
    """

    def __init__(
        self,
        k: float = 15.0,  # reference value (min detectable drift in meters per sample)
        h: float = 150.0,  # decision threshold (meters accumulated)
        window_seconds: float = 300.0,  # sliding window duration
    ):
        """
        Args:
            k: Minimum per-sample drift magnitude to accumulate (meters).
               Set above typical GPS noise (~10m) to avoid false accumulation.
               Typical range: 10–30 meters for ADS-B data.
            h: Decision threshold (cumulative meters before alarm).
               Higher = fewer false alarms, slower detection.
            window_seconds: Sliding window duration. After this period, CUSUM
               statistics reset to prevent unbounded accumulation.
        """
        self.k = k
        self.h = h
        self.window_seconds = window_seconds

        # Per-track incremental state
        # icao24 -> {"comp_pos": [float]*3, "comp_neg": [float]*3,
        #            "window_start": int, "processed": int,
        #            "pos_out": [float], "neg_out": [float], "alarms_out": [int]}
        self._track_cusum: dict[str, dict] = {}

    def reset_track(self, icao24: str) -> None:
        """Clear saved incremental CUSUM state for a track."""
        self._track_cusum.pop(icao24, None)

    def detect(
        self, filter_states: list[FilterState], icao24: str = ""
    ) -> tuple[list[float], list[float], list[int]]:
        """Run two-sided windowed CUSUM on raw residual components.

        When icao24 is provided and has been seen before, only processes
        filter states newer than the last-processed index (incremental).

        Returns:
            cusum_pos: Max positive CUSUM across all components at each step
            cusum_neg: Max negative CUSUM across all components at each step
            alarms: Indices where CUSUM exceeded threshold
        """
        n = len(filter_states)
        if n < 2:
            return [0.0] * n, [0.0] * n, []

        n_components = 3

        # Check for saved incremental state
        saved = self._track_cusum.get(icao24) if icao24 else None

        if saved is not None and saved["processed"] > 0:
            old_n = saved["processed"]
            if old_n >= n:
                return (
                    list(saved["pos_out"]),
                    list(saved["neg_out"]),
                    list(saved["alarms_out"]),
                )

            # Continue from saved component values
            comp_pos = list(saved["comp_pos"])
            comp_neg = list(saved["comp_neg"])
            window_start_abs = saved["window_start"]
            pos_out = list(saved["pos_out"])
            neg_out = list(saved["neg_out"])
            alarms_out = list(saved["alarms_out"])

            for abs_i in range(old_n, n):
                dt = (
                    filter_states[abs_i].time - filter_states[window_start_abs].time
                ).total_seconds()

                if dt > self.window_seconds:
                    window_start_abs = abs_i
                    comp_pos = [0.0, 0.0, 0.0]
                    comp_neg = [0.0, 0.0, 0.0]
                    pos_out.append(0.0)
                    neg_out.append(0.0)
                    continue

                residuals = filter_states[abs_i].residual_raw
                if len(residuals) < 3:
                    pos_out.append(max(comp_pos) if comp_pos else 0.0)
                    neg_out.append(max(comp_neg) if comp_neg else 0.0)
                    continue

                max_pos = 0.0
                max_neg = 0.0
                fired = False

                for c in range(n_components):
                    if c == 0:
                        z = residuals[c] * 111320.0
                    elif c == 1:
                        z = residuals[c] * 111320.0 * 0.7
                    else:
                        z = residuals[c]

                    comp_pos[c] = max(0.0, comp_pos[c] + z - self.k)
                    comp_neg[c] = max(0.0, comp_neg[c] - z - self.k)

                    if comp_pos[c] > self.h or comp_neg[c] > self.h:
                        fired = True

                    max_pos = max(max_pos, comp_pos[c])
                    max_neg = max(max_neg, comp_neg[c])

                pos_out.append(max_pos)
                neg_out.append(max_neg)

                if fired:
                    alarms_out.append(abs_i)
                    comp_pos = [0.0, 0.0, 0.0]
                    comp_neg = [0.0, 0.0, 0.0]
                    window_start_abs = abs_i

            self._track_cusum[icao24] = {
                "comp_pos": comp_pos,
                "comp_neg": comp_neg,
                "window_start": window_start_abs,
                "processed": n,
                "pos_out": pos_out,
                "neg_out": neg_out,
                "alarms_out": alarms_out,
            }

            return pos_out, neg_out, alarms_out

        # --- Full path (first call or no icao24) ---
        cusum_pos_components = [[0.0] * n for _ in range(n_components)]
        cusum_neg_components = [[0.0] * n for _ in range(n_components)]

        cusum_pos = [0.0] * n
        cusum_neg = [0.0] * n
        alarms: list[int] = []

        window_start_idx = 0

        for i in range(1, n):
            dt = (
                filter_states[i].time - filter_states[window_start_idx].time
            ).total_seconds()

            if dt > self.window_seconds:
                window_start_idx = i
                for c in range(n_components):
                    cusum_pos_components[c][i] = 0.0
                    cusum_neg_components[c][i] = 0.0
                continue

            residuals = filter_states[i].residual_raw
            if len(residuals) < 3:
                continue

            max_pos = 0.0
            max_neg = 0.0
            fired = False

            for c in range(n_components):
                if c == 0:
                    z = residuals[c] * 111320.0
                elif c == 1:
                    z = residuals[c] * 111320.0 * 0.7
                else:
                    z = residuals[c]

                cusum_pos_components[c][i] = max(
                    0.0, cusum_pos_components[c][i - 1] + z - self.k
                )
                cusum_neg_components[c][i] = max(
                    0.0, cusum_neg_components[c][i - 1] - z - self.k
                )

                if cusum_pos_components[c][i] > self.h or cusum_neg_components[c][i] > self.h:
                    fired = True

                max_pos = max(max_pos, cusum_pos_components[c][i])
                max_neg = max(max_neg, cusum_neg_components[c][i])

            cusum_pos[i] = max_pos
            cusum_neg[i] = max_neg

            if fired:
                alarms.append(i)
                for c in range(n_components):
                    cusum_pos_components[c][i] = 0.0
                    cusum_neg_components[c][i] = 0.0
                window_start_idx = i

        # Save state for future incremental calls
        if icao24:
            last_pos = [cusum_pos_components[c][-1] for c in range(n_components)]
            last_neg = [cusum_neg_components[c][-1] for c in range(n_components)]
            self._track_cusum[icao24] = {
                "comp_pos": last_pos,
                "comp_neg": last_neg,
                "window_start": window_start_idx,
                "processed": n,
                "pos_out": cusum_pos,
                "neg_out": cusum_neg,
                "alarms_out": alarms,
            }

        return cusum_pos, cusum_neg, alarms

    def annotate_result(
        self, result: DetectionResult, cusum_pos: list[float], cusum_neg: list[float], alarms: list[int]
    ) -> DetectionResult:
        """Annotate a Kalman DetectionResult with CUSUM scores and drift flags."""
        for idx in alarms:
            if idx < len(result.filter_states):
                fs = result.filter_states[idx]
            else:
                continue

            sv = None
            for s in result.track.states:
                if s.time == fs.time:
                    sv = s
                    break

            if sv is None:
                continue

            cusum_max = max(cusum_pos[idx], cusum_neg[idx])
            region = _classify_region(sv.latitude, sv.longitude)

            result.anomalies.append(
                AnomalyFlag(
                    icao24=sv.icao24,
                    callsign=sv.callsign,
                    time=sv.time,
                    latitude=sv.latitude,
                    longitude=sv.longitude,
                    altitude=sv.altitude,
                    flag_type="cusum_drift",
                    mahalanobis_distance=fs.innovation,
                    cusum_score=cusum_max,
                    residual_components=fs.residual_raw,
                    region=region,
                    severity=min(1.0, cusum_max / self.h),
                )
            )

        result.flagged = result.flagged or len(alarms) > 0

        # Update filter states with CUSUM values
        for i, fs in enumerate(result.filter_states):
            if i < len(cusum_pos):
                fs.cusum_positive = cusum_pos[i]
                fs.cusum_negative = cusum_neg[i]

        return result

    def process_result(self, result: DetectionResult) -> DetectionResult:
        """Full pipeline: run CUSUM on a Kalman DetectionResult, annotate flags."""
        cusum_pos, cusum_neg, alarms = self.detect(
            result.filter_states, icao24=result.track.icao24
        )
        return self.annotate_result(result, cusum_pos, cusum_neg, alarms)


def _classify_region(lat: float, lon: float) -> str:
    from config import config as cfg

    for name, (mn_lat, mx_lat, mn_lon, mx_lon) in cfg.regions.items():
        if mn_lat <= lat <= mx_lat and mn_lon <= lon <= mx_lon:
            return name
    return "unknown"


def compute_arl(h: float, k: float) -> float:
    """Approximate ARL₀ (average run length under H₀) for two-sided CUSUM.

    Uses Siegmund's approximation. Returns expected number of samples
    until false alarm when no drift is present.
    """
    if k <= 0:
        return float("inf")
    # Siegmund approximation for ARL of two-sided CUSUM
    delta = 0.583  # Siegmund constant
    arl = (np.exp(2 * k * (h + delta)) - 1) / (2 * k**2) - (h + delta) / k
    return max(1.0, arl)
