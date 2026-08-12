"""SGP4 propagation wrapper (python-sgp4)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import numpy as np
from sgp4.api import Satrec, jday

from orbital.models import GpElement, OrbitState


def _ensure_utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def satrec_from_element(el: GpElement) -> Satrec:
    """Build Satrec from TLE lines. Raises ValueError on parse failure."""
    sat = Satrec.twoline2rv(el.line1.strip(), el.line2.strip())
    return sat


def propagate_at(sat: Satrec, t: datetime, norad_id: int = 0) -> OrbitState:
    """Propagate to time t → TEME r (km), v (km/s)."""
    t = _ensure_utc(t)
    jd, fr = jday(
        t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond * 1e-6
    )
    err, r, v = sat.sgp4(jd, fr)
    if err != 0:
        return OrbitState(
            time=t,
            r_km=(0.0, 0.0, 0.0),
            v_km_s=(0.0, 0.0, 0.0),
            norad_id=norad_id,
            valid=False,
            error=f"sgp4_error_{err}",
        )
    return OrbitState(
        time=t,
        r_km=(float(r[0]), float(r[1]), float(r[2])),
        v_km_s=(float(v[0]), float(v[1]), float(v[2])),
        norad_id=norad_id,
        valid=True,
    )


def propagate_series(
    el: GpElement,
    times: Iterable[datetime],
) -> list[OrbitState]:
    """Propagate element set over a list of times."""
    sat = satrec_from_element(el)
    return [propagate_at(sat, t, norad_id=el.norad_id) for t in times]


def apply_impulsive_dv(
    state: OrbitState,
    dv_km_s: np.ndarray,
) -> OrbitState:
    """Return copy of state with velocity += dv (TEME km/s)."""
    if not state.valid:
        return state
    v = np.array(state.v_km_s, dtype=float) + np.asarray(dv_km_s, dtype=float)
    return OrbitState(
        time=state.time,
        r_km=state.r_km,
        v_km_s=(float(v[0]), float(v[1]), float(v[2])),
        norad_id=state.norad_id,
        valid=True,
    )


def rtn_basis(r_km: np.ndarray, v_km_s: np.ndarray) -> np.ndarray:
    """3x3 matrix with columns R, T, N unit vectors in TEME."""
    r = np.asarray(r_km, dtype=float)
    v = np.asarray(v_km_s, dtype=float)
    r_hat = r / (np.linalg.norm(r) + 1e-15)
    h = np.cross(r, v)
    n_hat = h / (np.linalg.norm(h) + 1e-15)
    t_hat = np.cross(n_hat, r_hat)
    t_hat = t_hat / (np.linalg.norm(t_hat) + 1e-15)
    # columns R, T, N
    return np.column_stack([r_hat, t_hat, n_hat])
