"""NASA ISS OEM parser + dual-source residual tests (fixture, offline)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

os.environ["ORBITAL_SOURCE"] = "fixture"
os.environ["ORBITAL_OEM_SOURCE"] = "fixture"

from orbital.frames import FRAME_DISCLOSURE, inertial_to_ecef_approx
from orbital.models import OrbitState
from orbital.oem import (
    ISS_NORAD_ID,
    get_iss_oem,
    load_fixture_oem,
    parse_oem,
    subsample_states,
)
from orbital.observe import (
    observe_iss_residual,
    residual_sample_ecef,
    residual_series_ecef,
)
from orbital.propagate import propagate_series
from orbital.synthetic import iss_element

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "orbital"
    / "fixtures"
    / "iss_oem_snippet.txt"
)


def test_parse_fixture_oem():
    eph = load_fixture_oem(FIXTURE)
    assert eph.meta.ref_frame.upper().startswith("EME")
    assert len(eph.states) >= 40
    s0 = eph.states[0]
    assert s0.valid
    assert s0.time.tzinfo is not None
    # LEO radius sanity
    r = np.linalg.norm(s0.r_km)
    assert 6400 < r < 7200
    v = np.linalg.norm(s0.v_km_s)
    assert 6.5 < v < 8.5


def test_parse_oem_meta_and_stride():
    text = FIXTURE.read_text()
    eph = parse_oem(text)
    assert eph.meta.object_name
    assert eph.meta.start_time is not None or eph.states
    sub = subsample_states(eph.states, 10)
    assert len(sub) == 10
    assert sub[0].time == eph.states[0].time
    assert sub[-1].time == eph.states[-1].time


def test_get_iss_oem_fixture_env():
    eph = get_iss_oem(source="fixture", max_states=20)
    assert eph.meta.source_mode == "fixture"
    assert len(eph.states) == 20
    assert eph.states[0].norad_id == ISS_NORAD_ID


def test_ecef_rotation_preserves_radius():
    t = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)
    r = (7000.0, 0.0, 0.0)
    e = inertial_to_ecef_approx(r, t)
    assert abs(np.linalg.norm(e) - 7000.0) < 1e-9


def test_residual_ecef_zero_when_same():
    t = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)
    st = OrbitState(
        time=t, r_km=(6500.0, 1000.0, -500.0), v_km_s=(1.0, 7.0, 0.5), valid=True
    )
    s = residual_sample_ecef(st, st)
    assert s.magnitude_km < 1e-9


def test_observe_iss_residual_fixture():
    result = observe_iss_residual(n_samples=40, oem_source="fixture")
    assert result.norad_id == ISS_NORAD_ID
    assert result.meta.get("synthetic") is False
    assert result.meta.get("observation_source") == "nasa_iss_oem"
    assert result.meta.get("pipeline") == "oem_vs_sgp4_ecef"
    assert FRAME_DISCLOSURE in (result.meta.get("frame_disclosure") or "")
    assert len(result.residuals) == 40
    assert len(result.meta.get("trail") or []) >= 2
    mags = [s.magnitude_km for s in result.residuals if np.isfinite(s.magnitude_km)]
    assert len(mags) >= 20
    # Dual-source residual is model/frame floor — expect km-scale, not nan
    assert np.median(mags) < 500.0  # sanity upper bound
    assert np.mean(mags) >= 0.0


def test_observe_uses_matching_times():
    eph = get_iss_oem(source="fixture", max_states=15)
    times = [s.time for s in eph.states]
    # fixture TLE may be stale relative to OEM; still must propagate
    el = iss_element()
    # Prefer catalog ISS if available
    from orbital.ingest import get_backend

    for e in get_backend().list_elements(group="stations"):
        if e.norad_id == 25544:
            el = e
            break
    ref = propagate_series(el, times)
    assert len(ref) == len(eph.states)
    res = residual_series_ecef(ref, eph.states)
    assert len(res) == 15
    assert all(r.time == t for r, t in zip(res, times))
