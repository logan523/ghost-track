"""Alert clustering and deduplication for the triage layer.

Groups related anomaly flags into incidents using DBSCAN on
spatial + temporal features. One aircraft flagged repeatedly in
a short window = one incident, not N alerts.
"""

import logging
from datetime import datetime
from typing import Optional

import numpy as np
from sklearn.cluster import DBSCAN

from config import config
from detector.models import AnomalyFlag

logger = logging.getLogger(__name__)

# Approximate conversions for clustering
DEG_PER_KM = 1.0 / 111.32  # degrees latitude per km
SECONDS_PER_DAY = 86400.0


class AlertClusterer:
    """DBSCAN-based alert clustering for spatial + temporal deduplication."""

    def __init__(
        self,
        spatial_radius_km: float = 50.0,
        time_window_seconds: float = 900.0,
        min_samples: int = 2,
    ):
        self.spatial_radius_km = spatial_radius_km
        self.time_window_seconds = time_window_seconds
        self.min_samples = min_samples

    def _features(self, anomalies: list[AnomalyFlag]) -> np.ndarray:
        """Convert anomalies to normalized feature vectors for clustering.

        Features: [lat_deg, lon_deg, time_scaled]
        Spatial scaled to ~1.0 at radius, temporal scaled to ~1.0 at window.
        """
        if not anomalies:
            return np.empty((0, 3))

        times = np.array([a.time.timestamp() for a in anomalies])
        t0 = times.min()

        features = np.zeros((len(anomalies), 3))
        for i, a in enumerate(anomalies):
            features[i, 0] = a.latitude / (self.spatial_radius_km * DEG_PER_KM)
            features[i, 1] = a.longitude / (self.spatial_radius_km * DEG_PER_KM)
            features[i, 2] = (a.time.timestamp() - t0) / self.time_window_seconds

        return features

    def cluster(
        self, anomalies: list[AnomalyFlag], eps: float = 0.5
    ) -> list[list[AnomalyFlag]]:
        """Group anomalies into incident clusters.

        Args:
            anomalies: List of anomaly flags
            eps: DBSCAN epsilon in normalized feature space.
                 0.5 means ~half the spatial radius OR half the time window.

        Returns:
            List of clusters, each a list of AnomalyFlags in that incident.
            Unclustered anomalies (noise) are each in their own singleton cluster.
        """
        if len(anomalies) <= 1:
            return [[a] for a in anomalies]

        X = self._features(anomalies)
        db = DBSCAN(eps=eps, min_samples=self.min_samples, metric="euclidean")
        labels = db.fit_predict(X)

        clusters: dict[int, list[AnomalyFlag]] = {}
        for i, label in enumerate(labels):
            if label not in clusters:
                clusters[label] = []
            clusters[label].append(anomalies[i])

        # Sort clusters by size (largest first)
        result = sorted(clusters.values(), key=len, reverse=True)
        return result


def cluster_anomalies(
    anomalies: list[AnomalyFlag],
    spatial_radius_km: Optional[float] = None,
    time_window_seconds: Optional[float] = None,
) -> list[list[AnomalyFlag]]:
    """Convenience wrapper for clustering."""
    clusterer = AlertClusterer(
        spatial_radius_km=spatial_radius_km or config.cluster_spatial_radius_km,
        time_window_seconds=time_window_seconds or config.cluster_time_window_seconds,
    )
    return clusterer.cluster(anomalies)
