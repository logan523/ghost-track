"""Ingest backend tests."""

import json
from pathlib import Path

import httpx

from orbital.ingest import CelesTrakBackend, FixtureBackend, get_backend
from orbital.synthetic import iss_element


def test_fixture_backend_loads():
    b = FixtureBackend()
    els = b.list_elements()
    assert len(els) >= 1
    assert els[0].line1.startswith("1 ")
    st = b.status()
    assert st.mode == "FIXTURE"


def test_get_backend_default_fixture(monkeypatch):
    monkeypatch.delenv("ORBITAL_SOURCE", raising=False)
    b = get_backend()
    assert isinstance(b, FixtureBackend)


def test_celestrak_cache_hit_no_http(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    # Minimal OMM-like with TLE lines
    el = iss_element()
    payload = [
        {
            "NORAD_CAT_ID": el.norad_id,
            "OBJECT_NAME": el.name,
            "EPOCH": "2024-01-01T12:00:00",
            "TLE_LINE1": el.line1,
            "TLE_LINE2": el.line2,
            "INCLINATION": 51.6,
            "MEAN_MOTION": 15.7,
            "ECCENTRICITY": 0.0006,
        }
    ]
    (cache / "stations.json").write_text(json.dumps(payload))

    def boom(*_a, **_k):
        raise AssertionError("HTTP should not be called on cache hit")

    monkeypatch.setattr(httpx.Client, "get", boom)
    b = CelesTrakBackend(group="stations", cache_dir=cache, ttl_s=99999)
    els = b.list_elements()
    assert len(els) >= 1
    assert b.status().mode == "LIVE_CELESTRAK"


def test_celestrak_403_degraded(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()

    class FakeResp:
        status_code = 403
        text = "blocked"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get(self, url):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", FakeClient)
    b = CelesTrakBackend(group="stations", cache_dir=cache, ttl_s=1)
    els = b.list_elements()
    assert len(els) >= 1  # fixture fallback
    assert b.status().mode == "DEGRADED"
