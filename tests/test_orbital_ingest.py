"""Ingest backend tests."""

from pathlib import Path

import httpx

from orbital.ingest import (
    CelesTrakBackend,
    FixtureBackend,
    get_backend,
    parse_tle_blob,
)
from orbital.synthetic import iss_element


def test_fixture_backend_loads():
    b = FixtureBackend()
    els = b.list_elements()
    assert len(els) >= 10  # multi-object stations fixture
    assert els[0].line1.startswith("1 ")
    st = b.status()
    assert st.mode == "FIXTURE"


def test_get_backend_default_celestrak(monkeypatch):
    monkeypatch.delenv("ORBITAL_SOURCE", raising=False)
    b = get_backend()
    assert isinstance(b, CelesTrakBackend)


def test_get_backend_fixture_env(monkeypatch):
    monkeypatch.setenv("ORBITAL_SOURCE", "fixture")
    b = get_backend()
    assert isinstance(b, FixtureBackend)


def test_parse_tle_blob():
    el = iss_element()
    text = f"{el.name}\n{el.line1}\n{el.line2}\n"
    out = parse_tle_blob(text)
    assert len(out) == 1
    assert out[0].norad_id == 25544


def test_celestrak_cache_hit_no_http(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    el = iss_element()
    (cache / "stations.tle").write_text(f"{el.name}\n{el.line1}\n{el.line2}\n")

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


def test_apply_limit_cap():
    b = FixtureBackend()
    els = b.list_elements(limit=3)
    assert len(els) == 3
