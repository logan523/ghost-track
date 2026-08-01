"""GPSJam.org interference data integration.

Provides known GNSS interference zones as GeoJSON for map overlay.
Bases interference data on GPSJam.org's documented patterns, updated
with known 2026 jamming events (Operation Epic Fury aftermath, etc.).

GPSJam.org does not provide a public API. This module serves:
1. A static GeoJSON file of known persistent jamming zones
2. Per-region baseline interference levels for triage enrichment

Data sources:
- GPSJam.org daily maps (public, visual)
- 2026 maritime GPS jamming events (Windward, Lloyd's List)
- Stanford GPS Lab validated incidents
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def get_interference_geojson() -> dict:
    """Return a GeoJSON FeatureCollection of known GNSS interference zones.

    These are persistent/historical jamming zones documented by GPSJam.org
    and validated incidents. Updated as new events are confirmed.
    """
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": "Kaliningrad Jamming",
                    "level": "high",
                    "description": "Persistent GNSS jamming from Kaliningrad region. Active since 2022. Affects Baltic Sea airspace up to FL400.",
                    "source": "GPSJam.org daily maps",
                    "color": "#e05545",
                    "opacity": 0.15,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [19.0, 54.0], [23.0, 54.0], [23.0, 56.5],
                        [19.0, 56.5], [19.0, 54.0],
                    ]],
                },
            },
            {
                "type": "Feature",
                "properties": {
                    "name": "Eastern Mediterranean Jamming",
                    "level": "medium",
                    "description": "Intermittent GNSS jamming from Eastern Libya and Cyprus vicinity. Episodic, not continuous.",
                    "source": "GPSJam.org daily maps",
                    "color": "#c4933b",
                    "opacity": 0.12,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [32.0, 34.0], [36.0, 34.0], [36.0, 38.0],
                        [32.0, 38.0], [32.0, 34.0],
                    ]],
                },
            },
            {
                "type": "Feature",
                "properties": {
                    "name": "Taiwan Strait Interference",
                    "level": "medium",
                    "description": "GPS interference in contested airspace. Correlates with military exercises and elevated tensions.",
                    "source": "GPSJam.org daily maps",
                    "color": "#c4933b",
                    "opacity": 0.12,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [118.0, 22.0], [122.0, 22.0], [122.0, 26.0],
                        [118.0, 26.0], [118.0, 22.0],
                    ]],
                },
            },
            {
                "type": "Feature",
                "properties": {
                    "name": "Arabian Peninsula Jamming (2026)",
                    "level": "high",
                    "description": "Post-Operation Epic Fury jamming clusters. 1,735 interference events recorded Feb-Mar 2026 across Saudi Arabia, Kuwait, UAE, Qatar, Oman, Iran.",
                    "source": "Windward, Lloyd's List (2026)",
                    "color": "#e05545",
                    "opacity": 0.15,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [45.0, 22.0], [58.0, 22.0], [58.0, 32.0],
                        [45.0, 32.0], [45.0, 22.0],
                    ]],
                },
            },
        ],
    }


def classify_interference(lat: float, lon: float) -> dict:
    """Classify a position's interference level based on known zones.

    Returns: {"level": "high"|"medium"|"low"|"unknown", "zone": str, "description": str}
    """
    zones = [
        ("high", "Kaliningrad Jamming", 19.0, 23.0, 54.0, 56.5),
        ("medium", "E. Mediterranean", 32.0, 36.0, 34.0, 38.0),
        ("medium", "Taiwan Strait", 118.0, 122.0, 22.0, 26.0),
        ("high", "Arabian Peninsula (2026)", 45.0, 58.0, 22.0, 32.0),
    ]

    for level, name, min_lon, max_lon, min_lat, max_lat in zones:
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return {
                "level": level,
                "zone": name,
                "description": f"Position falls within documented {level}-interference zone: {name}.",
            }

    return {
        "level": "unknown",
        "zone": None,
        "description": "Position is outside known interference zones. If anomalies are present, they warrant investigation.",
    }


def get_gpsjam_context(lat: float, lon: float) -> str:
    """Build a triage-agent prompt fragment with GPSJam context."""
    info = classify_interference(lat, lon)

    if info["level"] == "high":
        return (
            f"GPSJam context: This position is in a HIGH-interference zone ({info['zone']}). "
            "Anomalies here are consistent with known jamming patterns. Severity should be assessed "
            "against this elevated baseline rather than treated as novel events."
        )
    elif info["level"] == "medium":
        return (
            f"GPSJam context: This position is in a MEDIUM-interference zone ({info['zone']}). "
            "Intermittent jamming is documented here. Consider whether the anomaly pattern matches "
            "known episodic interference or represents something new."
        )
    else:
        return (
            "GPSJam context: This position is OUTSIDE known interference zones. "
            "Anomalies here are UNEXPLAINED by documented jamming patterns and warrant "
            "closer investigation than anomalies in known jamming areas."
        )
