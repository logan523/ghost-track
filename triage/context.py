"""GPSJam.org context enrichment for triage agent.

Provides per-region baseline/severity calibration instead of a single
global threshold. Some regions have jamming as the current norm, not a
rare exception — naive "anomalies are rare, escalate everything" logic
breaks in these areas.

Data is from GPSJam.org public maps and the Stanford GPS Lab.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Per-region jamming baseline data.
# These are approximate heuristics from public GPSJam.org data.
# Update before publishing results against current GPSJam maps.
REGION_CONTEXT: dict = {
    "baltic_sea": {
        "description": "Baltic Sea region — persistent GNSS jamming from Kaliningrad",
        "base_jamming_level": "high",  # jamming is the norm here
        "typical_affected_altitude_m": (0, 40000),
        "known_jammer_locations": ["Kaliningrad, Russia"],
        "base_anomaly_rate": 0.15,  # ~15% of tracks show anomalies (baseline)
        "severity_modifier": 0.7,  # reduce severity: jamming is expected
        "notes": (
            "GPSJam.org consistently shows high interference in this region. "
            "Jamming is the norm, not an exception — severity scores should "
            "be calibrated against this elevated baseline."
        ),
    },
    "eastern_med": {
        "description": "Eastern Mediterranean — intermittent GNSS jamming",
        "base_jamming_level": "medium",
        "typical_affected_altitude_m": (0, 30000),
        "known_jammer_locations": ["Eastern Libya", "Cyprus vicinity"],
        "base_anomaly_rate": 0.05,
        "severity_modifier": 1.0,
        "notes": (
            "Intermittent jamming reported. Severity should account for "
            "the episodic nature — sustained anomalies are more significant "
            "than isolated flags."
        ),
    },
    "taiwan_strait": {
        "description": "Taiwan Strait — contested airspace with GPS interference",
        "base_jamming_level": "medium",
        "typical_affected_altitude_m": (0, 35000),
        "known_jammer_locations": ["Multiple sources in vicinity"],
        "base_anomaly_rate": 0.08,
        "severity_modifier": 1.1,  # slightly elevated: contested airspace
        "notes": (
            "Contested airspace with documented GPS interference. "
            "Anomalies here should be interpreted in the context of "
            "ongoing military activity and electronic warfare exercises."
        ),
    },
    "denver": {
        "description": "Denver (KDEN) area — normally quiet, 2022 incident was notable",
        "base_jamming_level": "low",
        "typical_affected_altitude_m": (0, 20000),
        "known_jammer_locations": [],
        "base_anomaly_rate": 0.001,  # typically very quiet
        "severity_modifier": 2.0,  # anomalies here are very unusual
        "notes": (
            "Normally quiet region. The Oct 2022 Stanford-validated KDEN "
            "incident was exceptional. Anomalies here are highly significant."
        ),
    },
    "unknown": {
        "description": "Unknown/unclassified region",
        "base_jamming_level": "unknown",
        "typical_affected_altitude_m": (0, 40000),
        "known_jammer_locations": [],
        "base_anomaly_rate": 0.01,
        "severity_modifier": 1.0,
        "notes": "No regional baseline data available. Using default thresholds.",
    },
}


def get_region_context(region: str) -> dict:
    """Get the GPSJam/context baseline for a region."""
    return REGION_CONTEXT.get(region, REGION_CONTEXT["unknown"])


def calibrate_severity(raw_severity: float, region: str) -> float:
    """Apply per-region baseline calibration to a severity score.

    In high-jamming regions (Baltic), raw severity is reduced because
    anomalies are expected. In normally-quiet regions (Denver), severity
    is amplified because anomalies are unusual.

    Returns calibrated severity (0–1), clamped.
    """
    ctx = get_region_context(region)
    calibrated = raw_severity * ctx["severity_modifier"]
    return max(0.0, min(1.0, calibrated))


def build_context_prompt(region: str) -> str:
    """Build a context string for the triage agent prompt."""
    ctx = get_region_context(region)
    return (
        f"Region: {region}\n"
        f"Context: {ctx['description']}\n"
        f"Baseline jamming level: {ctx['base_jamming_level']}\n"
        f"Typical anomaly rate: {ctx['base_anomaly_rate']:.1%}\n"
        f"Notes: {ctx['notes']}\n"
        f"Severity calibration: anomalies in this region are "
        f"{'expected/normal' if ctx['base_jamming_level'] == 'high' else 'notable' if ctx['base_jamming_level'] == 'medium' else 'highly unusual'}."
    )
