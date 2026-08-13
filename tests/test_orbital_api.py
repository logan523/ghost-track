"""API route tests."""

import os

import pytest
from fastapi.testclient import TestClient

# Force offline fixture for API unit tests (no network)
os.environ["ORBITAL_SOURCE"] = "fixture"

from server import app  # noqa: E402

client = TestClient(app)


def test_health():
    r = client.get("/orbital/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "orbit-ghost"
    assert body["mode"] in ("FIXTURE", "LIVE_CELESTRAK", "DEGRADED")
    assert body["catalog_count"] >= 1
    assert "group" in body


def test_catalog():
    r = client.get("/orbital/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 10
    assert "custody_tier" in body["objects"][0]


def test_groups():
    r = client.get("/orbital/groups")
    assert r.status_code == 200
    ids = {g["id"] for g in r.json()["groups"]}
    assert "stations" in ids
    assert "visual" in ids


def test_field_scan():
    r = client.post(
        "/orbital/field/scan",
        json={"group": "stations", "limit": 8, "orbit_samples": 24, "include_orbits": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["n_scanned"] >= 5
    assert len(body["objects"]) == body["n_scanned"]
    o = body["objects"][0]
    assert "orbit" in o and len(o["orbit"]) >= 8
    assert "custody_tier" in o
    assert "r_km" in o["orbit"][0]


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


def test_detect_by_norad_id():
    r = client.post(
        "/orbital/detect", json={"dv_m_s": 0.0, "n_samples": 60, "norad_id": 25544}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["norad_id"] == 25544
    assert "ISS" in body["name"].upper()
    assert body["flagged"] is False


def test_detect_unknown_norad_id():
    r = client.post(
        "/orbital/detect", json={"dv_m_s": 1.0, "n_samples": 60, "norad_id": 99999999}
    )
    assert r.status_code == 404


def test_detect_chart_series():
    r = client.post("/orbital/detect", json={"dv_m_s": 5.0, "n_samples": 120})
    assert r.status_code == 200
    chart = r.json()["chart"]
    assert len(chart) >= 2
    pt = chart[-1]
    assert set(pt) == {"t", "mag_km", "rtn_km", "cusum"}
    assert pt["mag_km"] is not None and pt["mag_km"] > 0
    assert pt["cusum"] > 0


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
