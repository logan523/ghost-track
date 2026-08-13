"""API tests for /orbital/observe/* (fixture OEM, offline)."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ["ORBITAL_SOURCE"] = "fixture"
os.environ["ORBITAL_OEM_SOURCE"] = "fixture"

from server import app  # noqa: E402

client = TestClient(app)


def test_observe_status():
    r = client.get("/orbital/observe/status")
    assert r.status_code == 200
    body = r.json()
    assert body["fixture_present"] is True
    assert body["iss_norad_id"] == 25544
    assert "frame_disclosure" in body


def test_health_includes_observe():
    r = client.get("/orbital/health")
    assert r.status_code == 200
    obs = r.json()["observe"]
    assert obs["oem_fixture"] is True
    assert obs["iss_norad_id"] == 25544


def test_observe_iss_fixture():
    r = client.post(
        "/orbital/observe/iss",
        json={
            "n_samples": 40,
            "oem_source": "fixture",
            "norad_id": 25544,
            "group": "stations",
            "k": 1.5,
            "h": 12.0,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synthetic"] is False
    assert body["mode"] == "NASA_OEM_VS_SGP4"
    assert body["norad_id"] == 25544
    assert "ISS" in body["name"].upper()
    assert body["residual_summary"]["n"] == 40
    assert body["residual_summary"]["max_magnitude_km"] >= 0
    assert len(body["chart"]) >= 2
    assert len(body["trail"]) >= 2
    assert "r_km" in body["trail"][0]["ref"]
    assert body["meta"]["observation_source"] == "nasa_iss_oem"
    assert body["meta"]["observation_mode"] == "fixture"
    assert "frame_disclosure" in body
    assert body["frame_disclosure"]


def test_observe_iss_bad_source():
    r = client.post(
        "/orbital/observe/iss",
        json={"n_samples": 20, "oem_source": "not-a-source"},
    )
    assert r.status_code == 400


def test_observe_latest_after_run():
    client.post(
        "/orbital/observe/iss",
        json={"n_samples": 20, "oem_source": "fixture"},
    )
    r = client.get("/orbital/observe/latest")
    assert r.status_code == 200
    assert r.json()["mode"] == "NASA_OEM_VS_SGP4"
