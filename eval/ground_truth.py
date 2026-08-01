"""Labeled ground-truth dataset for evaluation.

Builds a labeled dataset combining:
- Known-positive periods: documented real GNSS jamming/spoofing incidents
- Known-negative periods: quiet control periods in the same regions

Sources:
- Stanford GPS Lab 2022 KDEN incident (operationally validated)
- GPSJam.org documented jamming events
- Control periods with no reported activity
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import config

logger = logging.getLogger(__name__)


@dataclass
class GroundTruthLabel:
    """A single ground-truth label for a time/position window."""

    region: str
    time_start: datetime
    time_end: datetime
    is_anomalous: bool
    label: str  # human-readable description
    source: str  # "stanford_kden_2022", "gpsjam", "control"
    confidence: float = 1.0  # 0–1, how confident we are in this label


# Known incident periods from published, validated sources.
# These are documented real-world GNSS jamming/spoofing events.
KNOWN_INCIDENTS: list[dict] = [
    {
        "region": "denver",
        "time_start": "2022-10-17T12:00:00Z",
        "time_end": "2022-10-17T18:00:00Z",
        "label": "Stanford-validated KDEN airport GNSS jamming incident",
        "source": "stanford_kden_2022",
        "confidence": 1.0,
    },
    {
        "region": "baltic_sea",
        "time_start": "2024-01-01T00:00:00Z",
        "time_end": "2024-01-02T00:00:00Z",
        "label": "Baltic Sea GNSS jamming (GPSJam.org documented)",
        "source": "gpsjam",
        "confidence": 0.9,
    },
    {
        "region": "eastern_med",
        "time_start": "2024-03-15T00:00:00Z",
        "time_end": "2024-03-16T00:00:00Z",
        "label": "Eastern Mediterranean GNSS jamming (GPSJam.org documented)",
        "source": "gpsjam",
        "confidence": 0.9,
    },
    {
        "region": "taiwan_strait",
        "time_start": "2024-05-01T00:00:00Z",
        "time_end": "2024-05-02T00:00:00Z",
        "label": "Taiwan Strait GNSS jamming (GPSJam.org documented)",
        "source": "gpsjam",
        "confidence": 0.85,
    },
]

# Known-quiet control periods (same regions, no reported jamming).
# These are approximate — verify against GPSJam before publishing results.
CONTROL_PERIODS: list[dict] = [
    {
        "region": "denver",
        "time_start": "2022-10-10T12:00:00Z",
        "time_end": "2022-10-10T18:00:00Z",
        "label": "Denver quiet control (no reported jamming)",
        "source": "control",
        "confidence": 0.95,
    },
    {
        "region": "baltic_sea",
        "time_start": "2024-01-08T00:00:00Z",
        "time_end": "2024-01-09T00:00:00Z",
        "label": "Baltic Sea quiet control (no reported jamming)",
        "source": "control",
        "confidence": 0.9,
    },
    {
        "region": "eastern_med",
        "time_start": "2024-03-22T00:00:00Z",
        "time_end": "2024-03-23T00:00:00Z",
        "label": "Eastern Med quiet control (no reported jamming)",
        "source": "control",
        "confidence": 0.9,
    },
    {
        "region": "taiwan_strait",
        "time_start": "2024-05-08T00:00:00Z",
        "time_end": "2024-05-09T00:00:00Z",
        "label": "Taiwan Strait quiet control (no reported jamming)",
        "source": "control",
        "confidence": 0.85,
    },
]


def load_ground_truth() -> list[GroundTruthLabel]:
    """Load the labeled ground-truth dataset.

    Returns a combined list of known-positive (anomalous) and known-negative
    (control) periods with metadata.
    """
    labels: list[GroundTruthLabel] = []

    for inc in KNOWN_INCIDENTS:
        labels.append(
            GroundTruthLabel(
                region=inc["region"],
                time_start=datetime.fromisoformat(inc["time_start"]),
                time_end=datetime.fromisoformat(inc["time_end"]),
                is_anomalous=True,
                label=inc["label"],
                source=inc["source"],
                confidence=inc["confidence"],
            )
        )

    for ctrl in CONTROL_PERIODS:
        labels.append(
            GroundTruthLabel(
                region=ctrl["region"],
                time_start=datetime.fromisoformat(ctrl["time_start"]),
                time_end=datetime.fromisoformat(ctrl["time_end"]),
                is_anomalous=False,
                label=ctrl["label"],
                source=ctrl["source"],
                confidence=ctrl["confidence"],
            )
        )

    return labels


def label_anomalies(
    anomalies: list, ground_truth: list[GroundTruthLabel]
) -> list[dict]:
    """Match detected anomalies to ground-truth labels.

    An anomaly is a true positive if it falls within a known-incident
    time window AND region. It's a false positive if it falls within a
    control period. Anomalies outside both are unlabeled.
    """
    labeled = []
    for a in anomalies:
        a_time = a.time if hasattr(a, "time") else a["time"]
        a_region = a.region if hasattr(a, "region") else a.get("region", "")

        matched = False
        for gt in ground_truth:
            if a_region == gt.region and gt.time_start <= a_time <= gt.time_end:
                labeled.append(
                    {
                        "anomaly": a,
                        "is_true_positive": gt.is_anomalous,
                        "is_false_positive": not gt.is_anomalous,
                        "ground_truth_label": gt.label,
                        "ground_truth_source": gt.source,
                        "ground_truth_confidence": gt.confidence,
                    }
                )
                matched = True
                break

        if not matched:
            labeled.append(
                {
                    "anomaly": a,
                    "is_true_positive": False,
                    "is_false_positive": False,
                    "ground_truth_label": "unlabeled",
                    "ground_truth_source": "none",
                    "ground_truth_confidence": 0.0,
                }
            )

    return labeled


def save_ground_truth(labels: list[GroundTruthLabel], path: Optional[str] = None):
    """Persist ground truth labels to disk."""
    p = Path(path or config.labeled_dir) / "ground_truth.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "region": l.region,
            "time_start": l.time_start.isoformat(),
            "time_end": l.time_end.isoformat(),
            "is_anomalous": l.is_anomalous,
            "label": l.label,
            "source": l.source,
            "confidence": l.confidence,
        }
        for l in labels
    ]
    p.write_text(json.dumps(data, indent=2))
    logger.info(f"Saved {len(labels)} ground truth labels to {p}")


def get_disclosure_text() -> str:
    """Return the circularity-risk disclosure for use in reports."""
    return (
        "Circularity disclosure: Ground-truth labels are sourced from GPSJam.org "
        "and the Stanford GPS Lab's validated 2022 KDEN incident. If GPSJam/SkAI "
        "data is also used for triage-agent context enrichment, those sources are "
        "not fully independent of the evaluation labels. This is a disclosed "
        "limitation, not disqualifying — the Stanford KDEN incident provides an "
        "independently validated anchor point."
    )
