"""Ghost Track configuration. All secrets come from environment variables."""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # OpenSky API (OAuth2 client credentials)
    opensky_client_id: str = os.getenv("OPENSKY_CLIENT_ID", "")
    opensky_client_secret: str = os.getenv("OPENSKY_CLIENT_SECRET", "")
    opensky_base_url: str = "https://opensky-network.org/api"

    # DeepSeek API (for triage agent — OpenAI-compatible)
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    triage_model: str = "deepseek-chat"

    # Detection thresholds
    kalman_mahalanobis_threshold: float = 3.0  # sigma for per-sample flag
    cusum_k: float = 15.0  # reference value (min detectable drift in meters/sample)
    cusum_h: float = 150.0  # decision threshold (cumulative meters before alarm)
    cusum_window_seconds: float = 300.0  # windowed CUSUM reset period

    # Clustering
    cluster_time_window_seconds: float = 900.0  # 15 min for dedup
    cluster_spatial_radius_km: float = 50.0

    # Regions of interest (bbox: min_lat, max_lat, min_lon, max_lon)
    regions: dict = field(default_factory=lambda: {
        "baltic_sea": (53.0, 62.0, 9.0, 31.0),
        "eastern_med": (31.0, 40.0, 25.0, 37.0),
        "taiwan_strait": (21.0, 27.0, 117.0, 123.0),
        "denver": (38.0, 41.0, -107.0, -103.0),  # For Stanford KDEN 2022 incident
    })

    # Data cache
    data_dir: str = os.path.join(os.path.dirname(__file__), "data")
    raw_dir: str = os.path.join(os.path.dirname(__file__), "data", "raw")
    labeled_dir: str = os.path.join(os.path.dirname(__file__), "data", "labeled")

    # Synthetic data
    synthetic_track_duration_seconds: float = 600.0
    synthetic_sample_rate_hz: float = 1.0

    @property
    def has_opensky_creds(self) -> bool:
        return bool(self.opensky_client_id and self.opensky_client_secret)

    @property
    def has_llm_key(self) -> bool:
        return bool(self.deepseek_api_key)


config = Config()
