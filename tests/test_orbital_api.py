"""API route tests."""

from fastapi.testclient import TestClient

# Import app after path is set
from server import app

client = TestClient(app)


def test_health():
    r = client.get("/orbital/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "orbit-ghost"
    assert body["mode"] in ("FIXTURE", "LIVE_CELESTRAK", "DEGRADED")


def test_catalog():
    r = client.get("/orbital/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1


def test_detect_clean():
    r = client.post("/orbital/detect", json={"dv_m_s": 0.0, "n_samples": 80})
    assert r.status_code == 200
    assert r.json()["flagged"] is False


def test_detect_maneuver():
    r = client.post("/orbital/detect", json={"dv_m_s": 5.0, "n_samples": 100})
    assert r.status_code == 200
    body = r.json()
    assert body["flagged"] is True
    assert "trail" in body
    assert len(body["trail"]) >= 2
    assert "r_km" in body["trail"][0]["ref"]
    assert "residual_km" in body["trail"][0]


def test_eval_run():
    r = client.post(
        "/orbital/eval/run",
        json={
            "n_clean": 5,
            "n_anomalous": 5,
            "dv_m_s": 5.0,
            "n_samples": 80,
            "seed": 7,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "f1" in body
    assert 0 <= body["f1"] <= 1

    r2 = client.get("/orbital/eval/latest")
    assert r2.status_code == 200
    assert r2.json()["f1"] == body["f1"]
