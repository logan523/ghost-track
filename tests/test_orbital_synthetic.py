"""Synthetic Δv generator tests."""

from orbital.synthetic import generate_clean_series, generate_maneuver_series


def test_zero_dv_identity():
    el, times, ref, obs = generate_maneuver_series(dv_m_s=0.0, n=30)
    for a, b in zip(ref, obs):
        assert a.r_km == b.r_km


def test_inject_changes_after_maneuver():
    _el, _t, ref, obs = generate_maneuver_series(dv_m_s=10.0, n=50, maneuver_fraction=0.4)
    i_m = int(0.4 * 50)
    # positions should diverge after maneuver
    diffs = [
        abs(obs[i].r_km[0] - ref[i].r_km[0])
        + abs(obs[i].r_km[1] - ref[i].r_km[1])
        + abs(obs[i].r_km[2] - ref[i].r_km[2])
        for i in range(i_m + 5, len(ref))
    ]
    assert max(diffs) > 0.01


def test_clean_series_length():
    el, times, states = generate_clean_series(n=15)
    assert len(times) == 15
    assert len(states) == 15
    assert el.norad_id == 25544
