"""Regression: detection cycles must not re-emit historical anomalies."""

from datetime import datetime, timedelta, timezone

from detector.cusum import CUSUMDetector
from detector.kalman import KalmanDetector
from detector.models import AircraftTrack, StateVector
from detector.scoring import compute_ghost_score, calibrate_severity_1_to_5


def _track_with_jump(n=30, jump_at=20):
    t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    states = []
    lat, lon = 55.0, 20.0
    for i in range(n):
        if i == jump_at:
            la, lo = lat + 0.5, lon + 0.5
        else:
            la, lo = lat + i * 0.001, lon + i * 0.001
        states.append(
            StateVector(
                icao24="abc123",
                callsign="TEST1",
                time=t0 + timedelta(seconds=i),
                latitude=la,
                longitude=lo,
                altitude=10000,
                velocity=200,
                heading=45,
                vertical_rate=0,
                region="baltic_sea",
            )
        )
    return AircraftTrack(icao24="abc123", callsign="TEST1", states=states)


def test_second_cycle_does_not_reemit_history():
    track = _track_with_jump()
    kalman = KalmanDetector()
    cusum = CUSUMDetector()
    r1 = cusum.process_result(kalman.process_track(track))
    assert len(r1.anomalies) > 0

    # One new clean sample
    last = track.states[-1]
    track.states.append(
        StateVector(
            icao24="abc123",
            callsign="TEST1",
            time=last.time + timedelta(seconds=1),
            latitude=last.latitude + 0.001,
            longitude=last.longitude + 0.001,
            altitude=10000,
            velocity=200,
            heading=45,
            vertical_rate=0,
            region="baltic_sea",
        )
    )
    r2 = cusum.process_result(kalman.process_track(track))
    # Must not re-emit the full first-cycle set
    assert len(r2.anomalies) < len(r1.anomalies)


def test_ghost_score_bounds():
    gs = compute_ghost_score(9, 300, 5, 8)
    assert 0 <= gs <= 100


def test_severity_calibration():
    raw, cal = calibrate_severity_1_to_5(4, 0.8)
    assert raw == 4
    assert 1 <= cal <= 5
