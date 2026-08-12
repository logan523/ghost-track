"""RTN residual tests."""

import numpy as np

from orbital.models import OrbitState
from orbital.residual import residual_sample, residual_series
from orbital.synthetic import generate_clean_series, generate_maneuver_series
from datetime import datetime, timezone


def test_zero_residual_clean():
    _el, _t, states = generate_clean_series(n=20)
    samples = residual_series(states, states)
    assert all(s.magnitude_km < 1e-9 for s in samples)


def test_along_track_grows_after_dv():
    _el, _t, ref, obs = generate_maneuver_series(dv_m_s=5.0, n=80)
    samples = residual_series(ref, obs)
    # Early samples near zero; late samples larger
    early = np.mean([s.magnitude_km for s in samples[:10]])
    late = np.mean([s.magnitude_km for s in samples[-10:]])
    assert late > early
    assert late > 0.1  # km


def test_pure_radial_component():
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    ref = OrbitState(time=t, r_km=(7000.0, 0.0, 0.0), v_km_s=(0.0, 7.5, 0.0), valid=True)
    obs = OrbitState(time=t, r_km=(7001.0, 0.0, 0.0), v_km_s=(0.0, 7.5, 0.0), valid=True)
    s = residual_sample(ref, obs)
    assert abs(s.radial_km - 1.0) < 0.05
    assert abs(s.along_track_km) < 0.05
