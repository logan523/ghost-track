"""CUSUM residual tests."""

from datetime import datetime, timedelta, timezone

from orbital.cusum import CUSUMConfig, ResidualCUSUM
from orbital.models import ResidualSample


def _series(mags: list[float]):
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        ResidualSample(
            time=t0 + timedelta(seconds=30 * i),
            radial_km=0.0,
            along_track_km=m,
            cross_track_km=0.0,
            magnitude_km=m,
        )
        for i, m in enumerate(mags)
    ]


def test_quiet_no_alarm():
    det = ResidualCUSUM(CUSUMConfig(k=0.1, h=1.0))
    _, _, flags = det.process(_series([0.0] * 50), norad_id=1, name="t")
    assert flags == []


def test_step_change_alarms():
    det = ResidualCUSUM(CUSUMConfig(k=0.05, h=0.3, window_samples=500))
    mags = [0.0] * 20 + [0.5] * 40
    _, _, flags = det.process(_series(mags), norad_id=2, name="t")
    assert len(flags) >= 1


def test_reset_isolation():
    det = ResidualCUSUM(CUSUMConfig(k=0.05, h=0.3))
    det.process(_series([1.0] * 30), norad_id=3, name="a")
    det.reset(3)
    _, _, flags = det.process(_series([0.0] * 10), norad_id=3, name="a")
    assert flags == []
