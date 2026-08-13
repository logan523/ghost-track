"""Light-weight frame helpers for dual-source residual.

OEM (NASA) is EME2000 / J2000-like inertial. SGP4 outputs TEME.
We map both into an approximate ECEF (no polar motion / nutation) via Earth
rotation angle so residual is not pure frame artifact. Residual floor is
disclosed in the observe API (model/frame km-scale, not radar noise).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Sequence

from sgp4.api import jday


def _ensure_utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def earth_rotation_angle_rad(t: datetime) -> float:
    """IAU 2000 Earth Rotation Angle (radians)."""
    t = _ensure_utc(t)
    jd, fr = jday(
        t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond * 1e-6
    )
    # days since J2000.0 TT≈UT1 for this precision
    tut1 = (jd - 2451545.0) + fr
    era = math.tau * (0.7790572732640 + 1.00273781191135448 * tut1)
    return era % math.tau


def inertial_to_ecef_approx(
    r_km: Sequence[float], t: datetime
) -> tuple[float, float, float]:
    """R3(ERA) · r — polar motion / nutation omitted (documented floor)."""
    a = earth_rotation_angle_rad(t)
    c, s = math.cos(a), math.sin(a)
    x, y, z = float(r_km[0]), float(r_km[1]), float(r_km[2])
    return (c * x + s * y, -s * x + c * y, z)


def ecef_to_inertial_approx(
    r_ecef_km: Sequence[float], t: datetime
) -> tuple[float, float, float]:
    a = earth_rotation_angle_rad(t)
    c, s = math.cos(a), math.sin(a)
    x, y, z = float(r_ecef_km[0]), float(r_ecef_km[1]), float(r_ecef_km[2])
    return (c * x - s * y, s * x + c * y, z)


FRAME_DISCLOSURE = (
    "Residual uses approximate ECEF alignment (Earth rotation only; no polar "
    "motion/nutation). OEM is NASA/JSC EME2000 state; reference is SGP4 TEME "
    "from public TLE. Residual floor includes model/frame mismatch — not a "
    "claim of radar observation noise."
)
