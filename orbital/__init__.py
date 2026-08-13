"""Orbit Ghost — orbital track anomaly / maneuver residual detection.

Space Domain Awareness companion to Ghost Track (ADS-B). Domain models and
pipelines are intentionally separate from detector/ (air) so NORAD/TLE state
never mixes with ICAO24 StateVector.

Architecture:
  Observed: NASA ISS OEM (EME2000) or SSCWeb
  Reference: CelesTrak/fixture TLE → SGP4 (TEME)
  ECEF-aligned RTN residual → CUSUM → flags
  Synthetic Δv remains training/eval only
"""


__all__ = ["__version__"]
__version__ = "0.1.0"
