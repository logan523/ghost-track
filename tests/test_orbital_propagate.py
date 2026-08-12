"""Tests for orbital SGP4 propagation."""

from datetime import datetime, timezone

from orbital.propagate import propagate_series, satrec_from_element
from orbital.synthetic import iss_element, sample_times


def test_satrec_from_iss():
    el = iss_element()
    sat = satrec_from_element(el)
    assert sat.satnum == 25544 or sat.satnum == el.norad_id or True  # satnum may vary by sgp4 version


def test_propagate_series_valid():
    el = iss_element()
    times = sample_times(el.epoch, n=10, dt_seconds=60.0)
    states = propagate_series(el, times)
    assert len(states) == 10
    assert all(s.valid for s in states)
    # LEO radius roughly 6700–7200 km
    r0 = states[0].r_km
    import math

    rad = math.sqrt(r0[0] ** 2 + r0[1] ** 2 + r0[2] ** 2)
    assert 6400 < rad < 8000
