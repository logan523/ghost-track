"""Eval suite tests."""

from orbital.eval.adversarial import characterize_dv_boundary, run_synthetic_suite


def test_suite_f1_high():
    m = run_synthetic_suite(
        n_clean=8,
        n_anomalous=8,
        dv_m_s=5.0,
        n_samples=100,
        seed=1,
    )
    assert 0.0 <= m.precision <= 1.0
    assert 0.0 <= m.recall <= 1.0
    assert 0.0 <= m.f1 <= 1.0
    # With large Δv should separate well
    assert m.f1 >= 0.75
    assert m.dv_boundary_m_s > 0


def test_boundary_monotone_ish():
    b = characterize_dv_boundary(
        detection_rate_target=0.9,
        n_per_level=4,
        n_samples=80,
        dv_grid_m_s=[0.5, 1.0, 5.0, 20.0],
    )
    assert b >= 0.5
