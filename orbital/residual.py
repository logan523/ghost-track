"""RTN residual series: observed − reference."""

from __future__ import annotations

import numpy as np

from orbital.models import OrbitState, ResidualSample
from orbital.propagate import rtn_basis


def residual_sample(ref: OrbitState, obs: OrbitState) -> ResidualSample:
    """Single RTN residual at matching time (uses ref.time)."""
    if not ref.valid or not obs.valid:
        return ResidualSample(
            time=ref.time,
            radial_km=float("nan"),
            along_track_km=float("nan"),
            cross_track_km=float("nan"),
            magnitude_km=float("nan"),
        )
    r_ref = np.array(ref.r_km, dtype=float)
    v_ref = np.array(ref.v_km_s, dtype=float)
    r_obs = np.array(obs.r_km, dtype=float)
    dr = r_obs - r_ref
    basis = rtn_basis(r_ref, v_ref)  # columns R,T,N
    rtn = basis.T @ dr
    mag = float(np.linalg.norm(rtn))
    return ResidualSample(
        time=ref.time,
        radial_km=float(rtn[0]),
        along_track_km=float(rtn[1]),
        cross_track_km=float(rtn[2]),
        magnitude_km=mag,
    )


def residual_series(
    ref_states: list[OrbitState],
    obs_states: list[OrbitState],
) -> list[ResidualSample]:
    """Pairwise residuals; lengths must match."""
    if len(ref_states) != len(obs_states):
        raise ValueError(
            f"ref/obs length mismatch: {len(ref_states)} vs {len(obs_states)}"
        )
    return [residual_sample(a, b) for a, b in zip(ref_states, obs_states)]


def magnitudes_km(samples: list[ResidualSample]) -> np.ndarray:
    return np.array([s.magnitude_km for s in samples], dtype=float)
