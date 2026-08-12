"""Orbit Ghost — orbital track anomaly / maneuver residual detection.

Space Domain Awareness companion to Ghost Track (ADS-B). Domain models and
pipelines are intentionally separate from detector/ (air) so NORAD/TLE state
never mixes with ICAO24 StateVector.

Architecture (P1):
  SourceBackend → SGP4 prop → synthetic Δv (eval) → RTN residual → CUSUM → flags
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
