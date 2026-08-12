# Ghost Track (+ Orbit Ghost)

Real-time **ADS-B** track-anomaly triage system, plus **Orbit Ghost** — a Space Domain Awareness residual / maneuver detector sharing the same sensor-to-decision + eval DNA.

**Air architecture:** Kalman filter + CUSUM detector (Python) → LLM triage agent → live map + alert feed.

**Space architecture (Orbit Ghost):** CelesTrak/fixture GP → SGP4 → RTN residual → CUSUM → FastAPI `/orbital/*` + synthetic eval suite.

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

Same control loop as Ghost Track, different measurement model: public TLEs / OMM → SGP4 → residual in RTN (radial / along-track / cross-track) → windowed CUSUM → operator flags (heuristic actions; LLM triage is P1.5).

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
ORBITAL_SOURCE=fixture .venv/bin/python -m pytest tests/test_orbital_*.py -q

# Run eval suite
ORBITAL_SOURCE=fixture .venv/bin/python -c "
from orbital.eval.adversarial import run_synthetic_suite
m = run_synthetic_suite(n_clean=20, n_anomalous=20, dv_m_s=2.0, seed=42)
print(f'F1={m.f1:.3f}  Δv_boundary={m.dv_boundary_m_s} m/s')
"

# API + Orbit Ghost 3D console
ORBITAL_SOURCE=fixture uvicorn server:app --port 8765
# UI:  http://localhost:8765/orbit
# GET  /orbital/health
# GET  /orbital/catalog
# POST /orbital/detect   {\"dv_m_s\": 5.0}  → trail + flags for globe
# POST /orbital/eval/run {\"n_clean\": 15, \"n_anomalous\": 15, \"dv_m_s\": 2.0}
```

**Backends:** `ORBITAL_SOURCE=fixture` (default, CI) | `celestrak` (2h disk cache, GROUP=stations). Space-Track is stubbed for P1.

**Package:** `orbital/` — does not reuse air `StateVector` / lat-lon Kalman (wrong physics for sparse GP data).

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
│   ├── propagate.py    # SGP4 wrapper
│   ├── residual.py     # RTN residual series
│   ├── cusum.py        # CUSUM on residual magnitude
│   ├── synthetic.py    # Δv injector for eval
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
