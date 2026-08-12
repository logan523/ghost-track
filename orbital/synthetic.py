"""Synthetic residual series via impulsive Δv injection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from orbital.models import GpElement, OrbitState
from orbital.propagate import apply_impulsive_dv, propagate_at, propagate_series, satrec_from_element


# ISS-like TLE (public catalog example; for offline fixtures / tests)
# Epoch embedded in lines — SGP4 uses element epoch; we sample relative times.
ISS_LINE1 = "1 25544U 98067A   24001.50000000  .00016717  00000-0  10270-3 0  9005"
ISS_LINE2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391584991"


def iss_element(name: str = "ISS (ZARYA)", norad_id: int = 25544) -> GpElement:
    return GpElement(
        norad_id=norad_id,
        name=name,
        epoch=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        line1=ISS_LINE1,
        line2=ISS_LINE2,
        source="fixture",
        inclination_deg=51.6416,
        mean_motion=15.72125391,
    )


def sample_times(
    t0: datetime,
    n: int = 120,
    dt_seconds: float = 30.0,
) -> list[datetime]:
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    return [t0 + timedelta(seconds=dt_seconds * i) for i in range(n)]


def generate_clean_series(
    el: Optional[GpElement] = None,
    n: int = 120,
    dt_seconds: float = 30.0,
    t0: Optional[datetime] = None,
) -> tuple[GpElement, list[datetime], list[OrbitState]]:
    """Reference = observed (zero residual)."""
    el = el or iss_element()
    t0 = t0 or el.epoch
    times = sample_times(t0, n=n, dt_seconds=dt_seconds)
    states = propagate_series(el, times)
    return el, times, states


def generate_maneuver_series(
    dv_m_s: float,
    el: Optional[GpElement] = None,
    n: int = 120,
    dt_seconds: float = 30.0,
    maneuver_fraction: float = 0.4,
    direction: str = "along_track",
    t0: Optional[datetime] = None,
) -> tuple[GpElement, list[datetime], list[OrbitState], list[OrbitState]]:
    """Return (el, times, ref_states, obs_states) with impulsive Δv on observed.

    Observed path: propagate cleanly until maneuver index, then add Δv in TEME
    and continue with Keplerian free-flight approx by re-integrating via
    successive SGP4 is wrong for post-Δv — for residual detection we use a
    simpler model:

    After maneuver index i_m:
      r_obs(t) = r_ref(t)
      v_obs at i_m += dv
      For t > i_m: r_obs(t) = r_ref(t) + (t - t_m) * dv_vector
      (constant velocity offset — along-track drift proxy for residual growth)

    This creates growing along-track residual without needing a full numerical
    integrator — appropriate for detector characterization.
    """
    el = el or iss_element()
    t0 = t0 or el.epoch
    times = sample_times(t0, n=n, dt_seconds=dt_seconds)
    ref = propagate_series(el, times)
    i_m = max(1, min(n - 2, int(maneuver_fraction * n)))

    # Direction of Δv in TEME at maneuver epoch
    st = ref[i_m]
    if not st.valid:
        raise RuntimeError("reference state invalid at maneuver epoch")
    r = np.array(st.r_km, dtype=float)
    v = np.array(st.v_km_s, dtype=float)
    r_hat = r / (np.linalg.norm(r) + 1e-15)
    h = np.cross(r, v)
    n_hat = h / (np.linalg.norm(h) + 1e-15)
    t_hat = np.cross(n_hat, r_hat)
    t_hat = t_hat / (np.linalg.norm(t_hat) + 1e-15)

    if direction == "radial":
        u = r_hat
    elif direction == "cross_track":
        u = n_hat
    else:
        u = t_hat

    dv_km_s = (dv_m_s / 1000.0) * u  # m/s → km/s
    t_m = times[i_m]

    obs: list[OrbitState] = []
    for i, (t, rs) in enumerate(zip(times, ref)):
        if i < i_m or not rs.valid:
            obs.append(rs)
            continue
        dt = (t - t_m).total_seconds()
        # position offset grows with constant velocity offset
        r_new = np.array(rs.r_km) + dv_km_s * dt
        v_new = np.array(rs.v_km_s) + dv_km_s
        obs.append(
            OrbitState(
                time=t,
                r_km=(float(r_new[0]), float(r_new[1]), float(r_new[2])),
                v_km_s=(float(v_new[0]), float(v_new[1]), float(v_new[2])),
                norad_id=el.norad_id,
                valid=True,
            )
        )

    return el, times, ref, obs
