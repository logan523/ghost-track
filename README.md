# Ghost Track (+ Orbit Ghost)

Real-time **ADS-B** track-anomaly triage system, plus **Orbit Ghost** — a Space Domain Awareness residual / maneuver detector sharing the same sensor-to-decision + eval DNA.

**Air architecture:** Kalman filter + CUSUM detector (Python) → LLM triage agent → live map + alert feed.

**Space architecture (Orbit Ghost):** dual-source residual — **NASA ISS OEM (JSC TOPO, real ephemeris)** as observed track vs **CelesTrak TLE → SGP4** as reference → ECEF-aligned RTN residual → CUSUM. Synthetic Δv remains for training/eval only.

## Phase 1 Results (Offline Validation)

| Metric | Value |
|--------|-------|
| Detector F1 | 0.949 |
| Detector Precision | 1.000 |
| Detector Recall | 0.903 |
| Clean vs Anomalous Separation | 7.7x |
| CUSUM Drift Boundary | >= 0.10 m/s at 95% detection |
| Hallucination Rate | 4.8% |

**The Kalman/CUSUM detector cleanly separates known-positive (spoofed/drifted) from known-negative (clean) aircraft tracks.** The separation ratio of 7.7x confirms the approach works before investing in Phase 2 live polling and serving infrastructure.

## Orbit Ghost (SDA residual detection)

Same control loop as Ghost Track, different measurement model.

### Real observations (primary demo path)

| Role | Source | Frame |
|------|--------|-------|
| **Observed** | [NASA ISS OEM](https://nasa-public-data.s3.amazonaws.com/iss-coords/current/ISS_OEM/ISS.OEM_J2K_EPH.txt) (JSC TOPO, CCSDS OEM, public S3, no key) | EME2000 |
| **Reference** | CelesTrak / fixture TLE → SGP4 | TEME |
| **Residual** | RTN after approximate ECEF alignment (Earth rotation only) | disclosed floor |

```bash
# Real dual-source residual (fixture OEM offline; live by default in auto)
ORBITAL_SOURCE=fixture ORBITAL_OEM_SOURCE=fixture .venv/bin/python -c "
from orbital.observe import observe_iss_residual
r = observe_iss_residual(n_samples=40, oem_source='fixture')
print(r.name, 'max_km=', r.meta['max_magnitude_km'], 'mode=', r.meta['observation_mode'])
print(r.meta['frame_disclosure'][:80], '...')
"

# Live NASA OEM (network)
# POST /orbital/observe/iss  {\"n_samples\": 90, \"oem_source\": \"auto\"}
```

**Honesty:** residual includes model + frame mismatch (no polar motion/nutation; SGP4 vs high-fidelity OEM). This is **not** radar observation noise and **not** a claim of real maneuver ground truth. Synthetic Δv remains labeled training only.

Optional fallback: NASA SSCWeb (`orbital/ssc.py`) for multi-mission location queries.

### Synthetic suite metrics (reproducible offline)

| Metric | Value |
|--------|-------|
| Detector F1 | 1.000 |
| Detector Precision | 1.000 |
| Detector Recall | 1.000 |
| Δv boundary (95% detection) | 0.05 m/s |
| Disclosure | Labels known by construction (synthetic Δv); not real on-orbit ground truth |

```bash
# Orbit Ghost tests
ORBITAL_SOURCE=fixture ORBITAL_OEM_SOURCE=fixture .venv/bin/python -m pytest tests/test_orbital_*.py -q

# Run eval suite
ORBITAL_SOURCE=fixture .venv/bin/python -c "
from orbital.eval.adversarial import run_synthetic_suite
m = run_synthetic_suite(n_clean=20, n_anomalous=20, dv_m_s=2.0, seed=42)
print(f'F1={m.f1:.3f}  Δv_boundary={m.dv_boundary_m_s} m/s')
"

# API + Orbit Ghost field console (live CelesTrak by default)
uvicorn server:app --port 8765
# Offline CI: ORBITAL_SOURCE=fixture ORBITAL_OEM_SOURCE=fixture uvicorn server:app --port 8765
# UI:  http://localhost:8765/orbit
# GET  /orbital/health?group=stations
# GET  /orbital/observe/status
# POST /orbital/observe/iss {\"n_samples\": 90, \"oem_source\": \"auto\"}  → NASA OEM vs SGP4
# POST /orbital/field/scan  {\"group\": \"stations\"}  → multi-track orbits + custody age
# POST /orbital/detect      {\"norad_id\": 25544, \"dv_m_s\": 5.0}  → synthetic Δv lab
# POST /orbital/eval/run    {\"n_clean\": 15, \"n_anomalous\": 15, \"dv_m_s\": 2.0}
```

**Backends:** `ORBITAL_SOURCE=celestrak` (**default**, live public TLEs via FORMAT=tle, 2h disk cache) | `fixture` (CI/offline multi-object stations JSON). OEM: `ORBITAL_OEM_SOURCE=auto|live|fixture|cache` (default auto). Groups: `stations`, `visual`, `active`, `starlink` (large groups capped at 300; override with `ORBITAL_MAX_OBJECTS`). Space-Track still stubbed.

**Package:** `orbital/` — `oem.py` / `observe.py` / `frames.py` / `ssc.py` for dual-source path; does not reuse air `StateVector` / lat-lon Kalman.

## Quick Start

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run Phase 1 validation
jupyter notebook notebooks/phase1_validation.ipynb

# Run pipeline smoke test
.venv/bin/python -c "
from detector.ingestion import _generate_synthetic_states, _group_into_tracks
from detector.kalman import KalmanDetector
from detector.cusum import CUSUMDetector
from eval.adversarial import AdversarialHarness, generate_clean_synthetic_track
from datetime import datetime, timezone

t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
t1 = datetime(2024, 1, 1, 12, 10, 0, tzinfo=timezone.utc)
states = _generate_synthetic_states(t0, t1, 53.0, 62.0, 9.0, 31.0)
tracks = _group_into_tracks(states)

kalman = KalmanDetector()
cusum = CUSUMDetector()
for track in tracks:
    result = cusum.process_result(kalman.process_track(track))
    if result.flagged:
        print(f'Track {track.icao24} ({track.callsign}): {result.anomaly_count} anomalies')

# Adversarial characterization
harness = AdversarialHarness(kalman=kalman, cusum=cusum)
clean = generate_clean_synthetic_track()
result = harness.characterize_boundary([clean])
print(f'Drift detection boundary: {result[\"boundary_m_s\"]:.4f} m/s')
"
```

## Project Structure

```
ghost-track/
├── detector/           # Air detection layer (Python)
│   ├── kalman.py       # 6-state Kalman filter position conformance
│   ├── cusum.py        # CUSUM/SPRT drift detection
│   ├── ingestion.py    # OpenSky REST API client (injectable backend)
│   ├── cross_validate.py  # ADS-B Exchange cross-check fallback
│   └── models.py       # ADS-B data models
├── orbital/            # Orbit Ghost (SDA residual / maneuver)
│   ├── oem.py          # NASA ISS OEM parse/fetch/cache
│   ├── observe.py      # dual-source OEM vs SGP4 residual
│   ├── frames.py       # ECEF alignment + disclosure
│   ├── ssc.py          # SSCWeb optional multi-mission
│   ├── propagate.py    # SGP4 wrapper
│   ├── residual.py     # RTN residual series
│   ├── cusum.py        # CUSUM on residual magnitude
│   ├── synthetic.py    # Δv injector for eval (training)
│   ├── detect.py       # pipeline
│   ├── ingest.py       # fixture + CelesTrak cache backends
│   ├── eval/           # F1 + Δv boundary suite
│   └── api.py          # FastAPI /orbital/*

├── triage/             # AI triage layer (air)
│   ├── agent.py        # Claude API triage agent (injectable LLM)
│   ├── clustering.py   # DBSCAN alert deduplication
│   └── context.py      # GPSJam.org per-region calibration
├── eval/               # Air evaluation harness
│   ├── ground_truth.py    # Labeled dataset (Stanford KDEN + GPSJam events)
│   ├── metrics.py         # Precision/recall/F1
│   ├── hallucination.py   # LLM-as-judge claim verification
│   └── adversarial.py     # Synthetic drift-injection test harness
├── notebooks/
│   └── phase1_validation.ipynb  # Phase 1 validation (run this first)
├── data/
│   └── orbital/        # fixtures + CelesTrak cache
│   ├── raw/            # Cached API responses
│   └── labeled/        # Ground truth labels
├── config.py           # Configuration (env vars for secrets)
└── requirements.txt
```

## Detection Methodology

- **Test 1 — Position conformance:** 6-state constant-velocity Kalman filter (lat, lon, alt, v_n, v_e, v_u). Flags when Mahalanobis distance between predicted and reported position exceeds 3σ.
- **Test 2 — CUSUM drift detection:** Windowed two-sided CUSUM on directional filter residuals (per-component: d_lat, d_lon, d_alt). Catches slow-drift GNSS attacks that evade per-sample thresholding.
- **Cross-validation fallback:** Second-source ADS-B data comparison (ADS-B Exchange / adsb.lol). Not true multilateration/TDOA.

## Prior Art & Novelty

- **Detection:** Grounded in Krozel et al. and the 2021 MDPI paper "ADS-B Crowd-Sensor Network and Two-Step Kalman Filter."
- **Triage:** Applies the proven SOC alert-triage agent pattern (Prophet Security, Dropzone AI, NVIDIA Morpheus) to ADS-B/aviation tracking — a pattern tested elsewhere but not in this domain.

## Design Decisions

- **Detection runs on a separate Python service** (FastAPI, Fly.io/Railway) because Cloudflare Python Workers can't run NumPy/SciPy in production.
- **No true multilateration/TDOA** unless OpenSky Trino access is separately granted (gated to university/gov affiliations).
- **No actuation/tasking capability** — stays human-tasked throughout.

## API Keys Needed (for Phase 2)

- OpenSky Network account (free tier): `OPENSKY_USERNAME`, `OPENSKY_PASSWORD`
- Anthropic API key: `ANTHROPIC_API_KEY`
- ADS-B Exchange RapidAPI key (optional): for cross-validation
