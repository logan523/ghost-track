"""Two-sided CUSUM on residual magnitude series (orbital domain).

Copy-adapted from detector/cusum.py for 1D residual magnitude (km).
Candidate for shared/stats after both domains stabilize — do not import
air FilterState here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np

from orbital.models import ManeuverFlag, ResidualSample


@dataclass
class CUSUMConfig:
    """k, h in residual-km units."""

    k: float = 0.05  # min per-sample drift to accumulate (km)
    h: float = 0.5  # decision threshold (km accumulated)
    window_samples: int = 200  # sliding window length in samples


class ResidualCUSUM:
    """Windowed two-sided CUSUM on residual magnitude."""

    def __init__(self, config: Optional[CUSUMConfig] = None):
        self.config = config or CUSUMConfig()
        # norad_id -> state
        self._state: dict[int, dict] = {}

    def reset(self, norad_id: int) -> None:
        self._state.pop(norad_id, None)

    def process(
        self,
        samples: list[ResidualSample],
        norad_id: int,
        name: str = "",
    ) -> tuple[list[float], list[float], list[ManeuverFlag]]:
        """Run CUSUM on magnitude series.

        Returns:
            cusum_pos, cusum_neg per sample, and ManeuverFlag list at alarms.
        """
        k = self.config.k
        h = self.config.h
        win = self.config.window_samples

        mags = np.array(
            [
                s.magnitude_km if np.isfinite(s.magnitude_km) else 0.0
                for s in samples
            ],
            dtype=float,
        )
        n = len(mags)
        if n == 0:
            return [], [], []

        # Use deviation from local median as score (positive residual growth)
        # For synthetic clean tracks mag ≈ 0; after Δv mag grows.
        # CUSUM on magnitude itself (one-sided positive + mirrored negative on -mag)
        s_pos = 0.0
        s_neg = 0.0
        pos_out: list[float] = []
        neg_out: list[float] = []
        flags: list[ManeuverFlag] = []
        window_start = 0

        for i, mag in enumerate(mags):
            if i - window_start >= win:
                s_pos = 0.0
                s_neg = 0.0
                window_start = i

            s_pos = max(0.0, s_pos + mag - k)
            s_neg = max(0.0, s_neg + (-mag) - k)  # rarely fires for mag≥0
            pos_out.append(s_pos)
            neg_out.append(s_neg)

            if s_pos > h or s_neg > h:
                score = max(s_pos, s_neg)
                samp = samples[i]
                severity = float(min(1.0, score / (h * 3.0)))
                flags.append(
                    ManeuverFlag(
                        norad_id=norad_id,
                        name=name,
                        time=samp.time,
                        flag_type="cusum_drift",
                        residual_magnitude_km=float(mag),
                        cusum_score=float(score),
                        residual_rtn_km=(
                            samp.radial_km,
                            samp.along_track_km,
                            samp.cross_track_km,
                        ),
                        severity=severity,
                        recommended_action=_heuristic_action(severity, mag),
                    )
                )
                # reset after alarm to avoid continuous re-fire
                s_pos = 0.0
                s_neg = 0.0

        return pos_out, neg_out, flags


def _heuristic_action(severity: float, mag_km: float) -> str:
    if severity >= 0.7 or mag_km >= 5.0:
        return "reobserve_priority"
    if severity >= 0.4:
        return "hold_custody_and_reobserve"
    return "monitor"
