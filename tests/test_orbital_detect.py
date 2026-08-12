"""Detection pipeline tests."""

from orbital.cusum import CUSUMConfig
from orbital.detect import detect_synthetic


def test_clean_unflagged():
    r = detect_synthetic(dv_m_s=0.0, n=100, cusum_config=CUSUMConfig(k=0.05, h=0.5))
    assert r.flagged is False
    assert r.flag_count == 0


def test_maneuver_flagged():
    r = detect_synthetic(dv_m_s=5.0, n=120, cusum_config=CUSUMConfig(k=0.05, h=0.5))
    assert r.flagged is True
    assert r.flag_count >= 1
    assert r.flags[0].recommended_action
