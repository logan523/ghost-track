# Ghost Track

Real-time ADS-B track-anomaly triage system. Demonstrates real-time systems engineering, applied detection theory, AI agent design, and rigorous evaluation methodology.

**Architecture:** Kalman filter + CUSUM detector (Python) → LLM triage agent (Claude API) → live map + alert feed (Cloudflare Workers, Phase 2).

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
├── detector/           # Detection layer (Python)
│   ├── kalman.py       # 6-state Kalman filter position conformance
│   ├── cusum.py        # CUSUM/SPRT drift detection
│   ├── ingestion.py    # OpenSky REST API client (injectable backend)
│   ├── cross_validate.py  # ADS-B Exchange cross-check fallback
│   └── models.py       # Data models
├── triage/             # AI triage layer
│   ├── agent.py        # Claude API triage agent (injectable LLM)
│   ├── clustering.py   # DBSCAN alert deduplication
│   └── context.py      # GPSJam.org per-region calibration
├── eval/               # Evaluation harness
│   ├── ground_truth.py    # Labeled dataset (Stanford KDEN + GPSJam events)
│   ├── metrics.py         # Precision/recall/F1
│   ├── hallucination.py   # LLM-as-judge claim verification
│   └── adversarial.py     # Synthetic drift-injection test harness
├── notebooks/
│   └── phase1_validation.ipynb  # Phase 1 validation (run this first)
├── data/
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
