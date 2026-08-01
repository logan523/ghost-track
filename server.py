"""Ghost Track — Phase 2 live polling server.

FastAPI app with background OpenSky polling, Kalman+CUSUM detection,
DeepSeek triage, and REST API for the live map frontend.
"""

import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import config
from detector.cusum import CUSUMDetector
from detector.ingestion import OpenSkyClient, _parse_state_vectors
from detector.kalman import KalmanDetector
from detector.models import AircraftTrack, StateVector
import random
import math
from triage.agent import TriageAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ghost-track")

app = FastAPI(title="Ghost Track", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# ── Global state ─────────────────────────────────────────────────────

POLL_REGIONS = ["baltic_sea", "eastern_med"]  # 2 regions per spec
POLL_INTERVAL = 5  # seconds between polls per region
DETECTION_INTERVAL = 5  # run detection every N polls
TRACK_BUFFER_MAX = 5000  # effectively unbounded (~7 hrs at 5s interval)
ANOMALY_TTL = 600  # seconds to keep anomalies in memory
REPORT_MAX = 50  # max triage reports to keep

# In-memory stores
tracks_buffer: dict[str, AircraftTrack] = {}  # icao24 -> track
filter_states_buffer: dict[str, dict] = {}    # icao24 -> {time_iso: {"P_lat_var": ..., "P_lon_var": ..., "innovation": ...}}
anomalies_store: list[dict] = []
reports_store: list[dict] = []
pipeline_stats = {
    "polls": 0,
    "states_total": 0,
    "anomalies_total": 0,
    "reports_total": 0,
    "last_poll": None,
    "last_detection": None,
    "errors": 0,
    "regions": {},
}

# Detectors (shared across polling loops)
kalman = KalmanDetector(mahalanobis_threshold=3.0)
cusum = CUSUMDetector(k=15.0, h=150.0, window_seconds=300.0)
triage_agent = TriageAgent()

# Synthetic aircraft fleet for demo mode (when OpenSky creds unavailable)
_synthetic_fleet: list[dict] = []
SYNTHETIC_FLEET_SIZE = 25
PAST_TRAIL_POINTS = 120  # ~10 minutes of pre-populated flight history


def _init_synthetic_fleet():
    """Seed a fleet of synthetic aircraft with point-to-point flight routes."""
    global _synthetic_fleet
    if _synthetic_fleet:
        return
    random.seed(7)
    callsigns = ["UAL", "DAL", "BAW", "LUF", "AFR", "KLM", "QTR", "EMI", "THY", "SIA",
                 "CPA", "JAL", "ANA", "QFA", "ETD", "VIR", "ACA", "SWR", "SAS", "FIN",
                 "AMX", "AVA", "LAN", "AZU", "GOL"]

    for region_name, idx_range in [("baltic_sea", range(SYNTHETIC_FLEET_SIZE)),
                                     ("eastern_med", range(SYNTHETIC_FLEET_SIZE, SYNTHETIC_FLEET_SIZE * 2))]:
        min_lat, max_lat, min_lon, max_lon = config.regions[region_name]
        for i in idx_range:
            # Pick two distant points in the region for the flight route
            origin_lat = random.uniform(min_lat + 1, max_lat - 1)
            origin_lon = random.uniform(min_lon + 1, max_lon - 1)
            dest_lat = random.uniform(min_lat + 1, max_lat - 1)
            dest_lon = random.uniform(min_lon + 1, max_lon - 1)
            while abs(dest_lat - origin_lat) < 2 and abs(dest_lon - origin_lon) < 2:
                dest_lat = random.uniform(min_lat + 1, max_lat - 1)
                dest_lon = random.uniform(min_lon + 1, max_lon - 1)

            # Aircraft is 30-70% through the route
            progress = random.uniform(0.3, 0.7)
            cur_lat = origin_lat + (dest_lat - origin_lat) * progress
            cur_lon = origin_lon + (dest_lon - origin_lon) * progress
            heading = math.degrees(math.atan2(dest_lon - origin_lon, dest_lat - origin_lat)) % 360
            speed = random.uniform(200, 280)

            cs_idx = i if i < 25 else (i - 25 + 12) % 25
            _synthetic_fleet.append({
                "icao24": f"SYN{i:04d}",
                "callsign": f"{callsigns[cs_idx]}{random.randint(100, 999)}",
                "lat": cur_lat,
                "lon": cur_lon,
                "alt": random.uniform(8000, 12000),
                "heading": heading,
                "speed": speed,
                "vr": random.uniform(-2, 2),
                "region": region_name,
                "origin_lat": origin_lat,
                "origin_lon": origin_lon,
                "dest_lat": dest_lat,
                "dest_lon": dest_lon,
                "first_poll": True,
            })


def _advance_synthetic(ac: dict, dt: float):
    """Advance a synthetic aircraft along its point-to-point route."""
    dlat_total = ac["dest_lat"] - ac["origin_lat"]
    dlon_total = ac["dest_lon"] - ac["origin_lon"]
    total_deg = math.sqrt(dlat_total**2 + dlon_total**2)
    if total_deg < 0.001:
        return

    dist_per_step = (ac["speed"] * dt) / 111320.0
    frac = dist_per_step / total_deg if total_deg > 0 else 0

    ac["lat"] += dlat_total * frac
    ac["lon"] += dlon_total * frac
    ac["alt"] += ac["vr"] * dt

    # Update heading toward destination
    ac["heading"] = math.degrees(math.atan2(
        ac["dest_lon"] - ac["lon"], ac["dest_lat"] - ac["lat"]
    )) % 360

    # Reached destination? Pick a new one
    rem_lat = abs(ac["dest_lat"] - ac["lat"])
    rem_lon = abs(ac["dest_lon"] - ac["lon"])
    if rem_lat < 0.15 and rem_lon < 0.15:
        ac["origin_lat"] = ac["lat"]
        ac["origin_lon"] = ac["lon"]
        min_lat2, max_lat2, min_lon2, max_lon2 = config.regions[ac["region"]]
        ac["dest_lat"] = random.uniform(min_lat2 + 1, max_lat2 - 1)
        ac["dest_lon"] = random.uniform(min_lon2 + 1, max_lon2 - 1)
        while abs(ac["dest_lat"] - ac["origin_lat"]) < 2 and abs(ac["dest_lon"] - ac["origin_lon"]) < 2:
            ac["dest_lat"] = random.uniform(min_lat2 + 1, max_lat2 - 1)
            ac["dest_lon"] = random.uniform(min_lon2 + 1, max_lon2 - 1)

    # Occasionally perturb altitude slightly
    if random.random() < 0.03:
        ac["vr"] += random.gauss(0, 0.5)
        ac["vr"] = max(-5, min(5, ac["vr"]))


# ── Background polling ────────────────────────────────────────────────

async def poll_region(client: OpenSkyClient, region_name: str):
    """Poll OpenSky for a single region, update track buffers."""
    min_lat, max_lat, min_lon, max_lon = config.regions[region_name]
    headers = client._headers()
    now = datetime.now(timezone.utc)
    got_real_data = False

    if client.has_credentials:
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.get(
                    f"{client.base_url}/states/all",
                    headers=headers,
                    params={
                        "lamin": min_lat, "lamax": max_lat,
                        "lomin": min_lon, "lomax": max_lon,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                raw_states = data.get("states", []) or []
                states = _parse_state_vectors(raw_states)

                for sv in states:
                    sv.region = region_name
                    key = sv.icao24
                    if key not in tracks_buffer:
                        tracks_buffer[key] = AircraftTrack(
                            icao24=sv.icao24, callsign=sv.callsign, states=[]
                        )
                    track = tracks_buffer[key]
                    track.states.append(sv)
                    if sv.callsign and sv.callsign.strip():
                        track.callsign = sv.callsign.strip()

                pipeline_stats["regions"][region_name] = {
                    "aircraft": len(states),
                    "last_poll": now.isoformat(),
                }
                logger.info(f"[{region_name}] {len(states)} aircraft")
                got_real_data = True

        except Exception as e:
            logger.error(f"[{region_name}] poll error: {e}")
            pipeline_stats["errors"] += 1

    if not got_real_data:
        # Synthetic demo data — always works, no credentials needed
        _init_synthetic_fleet()
        region_ac = [a for a in _synthetic_fleet if a["region"] == region_name]
        for ac in region_ac:
            key = ac["icao24"]
            if key not in tracks_buffer:
                tracks_buffer[key] = AircraftTrack(
                    icao24=ac["icao24"], callsign=ac["callsign"], states=[]
                )
            track = tracks_buffer[key]
            track.callsign = ac["callsign"]

            # Inject full past flight history on first poll
            if ac.get("first_poll"):
                ac["first_poll"] = False
                hr = math.radians(ac["heading"])
                cos_hr = math.cos(hr)
                sin_hr = math.sin(hr)
                cos_lat = math.cos(math.radians(ac["lat"]))
                for j in range(PAST_TRAIL_POINTS, 0, -1):
                    dt_back = j * 5
                    dist_deg = (ac["speed"] * dt_back) / 111320.0
                    past_lat = ac["lat"] - dist_deg * cos_hr
                    past_lon = ac["lon"] - dist_deg * sin_hr / cos_lat
                    past_time = now - timedelta(seconds=dt_back)
                    # Inject occasional position anomaly (~3% of points) for demo incidents
                    anomaly_shift = 0
                    if random.random() < 0.03:
                        anomaly_shift = random.uniform(0.02, 0.08)
                    sv = StateVector(
                        icao24=ac["icao24"],
                        callsign=ac["callsign"],
                        time=past_time,
                        latitude=past_lat + random.gauss(0, 0.00015) + anomaly_shift,
                        longitude=past_lon + random.gauss(0, 0.00015) + anomaly_shift * 0.5,
                        altitude=ac["alt"] + random.gauss(0, 15),
                        velocity=ac["speed"] + random.gauss(0, 4),
                        heading=(ac["heading"] + random.gauss(0, 1.5)) % 360,
                        vertical_rate=ac["vr"] + random.gauss(0, 0.3),
                        region=region_name,
                    )
                    track.states.append(sv)

            # Advance and add current position
            _advance_synthetic(ac, 5.0)
            sv = StateVector(
                icao24=ac["icao24"],
                callsign=ac["callsign"],
                time=now,
                latitude=ac["lat"],
                longitude=ac["lon"],
                altitude=ac["alt"],
                velocity=ac["speed"],
                heading=ac["heading"],
                vertical_rate=ac["vr"],
                region=region_name,
            )
            track.states.append(sv)

        pipeline_stats["regions"][region_name] = {
            "aircraft": len(region_ac),
            "last_poll": now.isoformat(),
            "synthetic": True,
        }
        logger.info(f"[{region_name}] {len(region_ac)} synthetic aircraft")


async def run_detection():
    """Run Kalman + CUSUM on all buffered tracks, triage new anomalies."""
    new_anomalies = []
    now = datetime.now(timezone.utc)

    # Only process tracks with >= 2 samples
    eligible = [t for t in tracks_buffer.values() if len(t.states) >= 2]
    if not eligible:
        return

    for track in eligible:
        try:
            result = kalman.process_track(track)
            result = cusum.process_result(result)

            # Store filter states for variance visualization
            fs_dict = {}
            for fs in result.filter_states:
                P = fs.P  # flattened 6x6 covariance
                fs_dict[fs.time.isoformat()] = {
                    "P_lat_var": float(P[0]) if len(P) > 0 else 0.0,   # deg^2
                    "P_lon_var": float(P[7]) if len(P) > 7 else 0.0,   # deg^2
                    "innovation": float(fs.innovation),
                }
            filter_states_buffer[track.icao24] = fs_dict

            for a in result.anomalies:
                new_anomalies.append({
                    "icao24": a.icao24,
                    "callsign": track.callsign,
                    "time": a.time.isoformat(),
                    "latitude": a.latitude,
                    "longitude": a.longitude,
                    "altitude": a.altitude,
                    "flag_type": a.flag_type,
                    "mahalanobis_distance": round(a.mahalanobis_distance, 1),
                    "cusum_score": round(a.cusum_score, 0),
                    "region": a.region,
                    "severity": round(a.severity, 2),
                })
        except Exception as e:
            logger.debug(f"Detection error for {track.icao24}: {e}")

    # Add to store with TTL pruning
    # Compute Ghost Score for each new anomaly
    for a in new_anomalies:
        a["_expires"] = time.time() + ANOMALY_TTL
        # Ghost Score: composite significance (0-100)
        # Mahalanobis component (0-40): normalized by 3σ threshold
        mahal_score = min(40, max(0, (a.get("mahalanobis_distance", 0) / 3.0) * 10))
        # CUSUM component (0-30): normalized by threshold
        cusum_score = min(30, max(0, (a.get("cusum_score", 0) / 150.0) * 10))
        # Persistence component (0-20): more flags on same aircraft = higher
        same_ac = sum(1 for x in anomalies_store if x.get("icao24") == a["icao24"])
        persistence_score = min(20, same_ac * 3)
        # Cluster density bonus (0-10): more anomalies in same region/time
        nearby = sum(1 for x in anomalies_store
                     if x.get("region") == a.get("region") and
                     abs(x.get("_stored_at", 0) - time.time()) < 300)
        density_score = min(10, nearby)
        a["ghost_score"] = round(mahal_score + cusum_score + persistence_score + density_score)
        a["_stored_at"] = time.time()
    anomalies_store.extend(new_anomalies)

    # Prune expired
    cutoff = time.time()
    anomalies_store[:] = [a for a in anomalies_store if a.get("_expires", 0) > cutoff]

    # Triage if we have enough new anomalies
    if len(new_anomalies) >= 5:
        try:
            from detector.models import AnomalyFlag
            flags = [
                AnomalyFlag(
                    icao24=a["icao24"], callsign=a.get("callsign", ""),
                    time=datetime.fromisoformat(a["time"]),
                    latitude=a["latitude"], longitude=a["longitude"],
                    altitude=a.get("altitude", 0),
                    flag_type=a["flag_type"],
                    mahalanobis_distance=a["mahalanobis_distance"],
                    cusum_score=a["cusum_score"],
                    residual_components=[0, 0, 0], region=a.get("region", ""),
                    severity=a.get("severity", 0),
                )
                for a in new_anomalies[:30]
            ]
            reports = triage_agent.triage(flags)
            for r in reports[:10]:
                report_dict = {
                    "incident_id": r.incident_id,
                    "aircraft_ids": r.aircraft_ids,
                    "time_start": r.time_start.isoformat(),
                    "time_end": r.time_end.isoformat(),
                    "region": r.region,
                    "anomaly_count": r.anomaly_count,
                    "severity_score": r.severity_score,
                    "summary": r.summary,
                    "recommended_action": r.recommended_action,
                    "_time": now.isoformat(),
                }
                reports_store.insert(0, report_dict)
                pipeline_stats["reports_total"] += 1
        except Exception as e:
            logger.error(f"Triage error: {e}")

    # Trim reports
    if len(reports_store) > REPORT_MAX:
        reports_store[:] = reports_store[:REPORT_MAX]

    pipeline_stats["anomalies_total"] += len(new_anomalies)
    pipeline_stats["last_detection"] = now.isoformat()

    # Prune stale tracks (no updates in 30 min)
    stale = [
        k for k, t in tracks_buffer.items()
        if t.states and (now - t.states[-1].time).total_seconds() > 1800
    ]
    for k in stale:
        del tracks_buffer[k]

    logger.info(f"Detection: {len(new_anomalies)} new anomalies from {len(eligible)} tracks")


async def polling_loop():
    """Main background loop: poll regions, periodically run detection."""
    client = OpenSkyClient()
    poll_count = 0

    while True:
        # Poll all regions concurrently
        tasks = [poll_region(client, r) for r in POLL_REGIONS]
        await asyncio.gather(*tasks, return_exceptions=True)

        pipeline_stats["polls"] += 1
        pipeline_stats["last_poll"] = datetime.now(timezone.utc).isoformat()
        poll_count += 1

        # Run detection every N polls
        if poll_count % DETECTION_INTERVAL == 0:
            await run_detection()

        await asyncio.sleep(POLL_INTERVAL)


# ── REST API ──────────────────────────────────────────────────────────

@app.get("/api/aircraft")
async def get_aircraft(region: Optional[str] = None):
    """Get current aircraft positions."""
    now = datetime.now(timezone.utc)
    aircraft = []

    for icao24, track in tracks_buffer.items():
        if not track.states:
            continue
        latest = track.states[-1]

        # Filter by region
        if region and latest.region != region:
            continue

        # Only include recent positions (< 2 min old)
        if (now - latest.time).total_seconds() > 120:
            continue

        # Check if this aircraft has active anomalies
        active_flags = [
            a for a in anomalies_store
            if a["icao24"] == icao24
        ]

        # Flight trail: all positions with Kalman variance
        fs_dict = filter_states_buffer.get(icao24, {})
        trail = []
        for s in track.states:
            pt = {
                "lat": s.latitude,
                "lon": s.longitude,
                "alt": s.altitude if not (s.altitude != s.altitude) else None,
            }
            # Attach Kalman variance if available for this timestamp
            fs = fs_dict.get(s.time.isoformat())
            if fs:
                pt["lat_std_m"] = round(float(fs["P_lat_var"]) ** 0.5 * 111320, 1)
                pt["lon_std_m"] = round(float(fs["P_lon_var"]) ** 0.5 * 111320 * 0.7, 1)
                pt["innovation"] = round(float(fs["innovation"]), 2)
            trail.append(pt)

        aircraft.append({
            "icao24": icao24,
            "callsign": track.callsign,
            "latitude": latest.latitude,
            "longitude": latest.longitude,
            "altitude": latest.altitude if not (latest.altitude != latest.altitude) else None,
            "velocity": latest.velocity if not (latest.velocity != latest.velocity) else None,
            "heading": latest.heading if not (latest.heading != latest.heading) else None,
            "on_ground": latest.on_ground,
            "region": latest.region,
            "flagged": len(active_flags) > 0,
            "anomaly_count": len(active_flags),
            "trail": trail,
            "last_seen": latest.time.isoformat(),
        })

    return {"aircraft": aircraft, "count": len(aircraft), "time": now.isoformat()}


@app.get("/api/anomalies")
async def get_anomalies(limit: int = 100, region: Optional[str] = None):
    """Get recent anomalies."""
    result = anomalies_store[:]
    if region:
        result = [a for a in result if a.get("region") == region]
    result = result[-limit:]
    return {"anomalies": result, "count": len(result)}


@app.get("/api/reports")
async def get_reports(limit: int = 20):
    """Get recent triage reports."""
    return {"reports": reports_store[:limit], "count": min(len(reports_store), limit)}


@app.get("/api/regions")
async def get_regions():
    """Get available regions with bounding boxes."""
    return {
        "regions": {
            name: {"min_lat": b[0], "max_lat": b[1], "min_lon": b[2], "max_lon": b[3]}
            for name, b in config.regions.items()
        },
        "active": POLL_REGIONS,
    }


@app.get("/api/stats")
async def get_stats():
    """Get pipeline statistics."""
    return {
        **pipeline_stats,
        "tracks_buffered": len(tracks_buffer),
        "anomalies_stored": len(anomalies_store),
        "reports_stored": len(reports_store),
        "poll_regions": POLL_REGIONS,
        "poll_interval_s": POLL_INTERVAL,
        "detection_interval_polls": DETECTION_INTERVAL,
    }


@app.get("/api/threats")
async def get_threats():
    """Per-region threat levels based on anomaly density vs baseline."""
    now = datetime.now(timezone.utc)
    threats = {}

    for region_name in POLL_REGIONS:
        # Count recent anomalies in this region (last 5 min)
        recent = [
            a for a in anomalies_store
            if a.get("region") == region_name
            and a.get("_expires", 0) > time.time()
        ]
        aircraft_count = sum(
            1 for t in tracks_buffer.values()
            if t.states and t.states[-1].region == region_name
            and (now - t.states[-1].time).total_seconds() < 120
        )

        # Compute threat level
        anomaly_rate = len(recent) / max(aircraft_count, 1)
        if anomaly_rate > 0.5:
            level, label = "critical", "Active jamming likely"
        elif anomaly_rate > 0.2:
            level, label = "elevated", "Above-normal anomaly rate"
        elif anomaly_rate > 0.05:
            level, label = "watch", "Slight increase in anomalies"
        else:
            level, label = "quiet", "Normal operations"

        threats[region_name] = {
            "level": level,
            "label": label,
            "anomaly_count": len(recent),
            "aircraft_count": aircraft_count,
            "anomaly_rate": round(anomaly_rate, 3),
        }

    return {"threats": threats, "time": now.isoformat()}


@app.get("/api/timeline")
async def get_timeline():
    """Anomaly frequency timeline (last 60 minutes, 1-min buckets)."""
    now = time.time()
    buckets = {}
    window = 3600  # 1 hour
    bucket_size = 60  # 1 minute

    for a in anomalies_store:
        stored = a.get("_stored_at", 0)
        age = now - stored
        if age > window:
            continue
        bucket = int(age / bucket_size)
        buckets[bucket] = buckets.get(bucket, 0) + 1

    timeline = []
    for i in range(int(window / bucket_size)):
        timeline.append({
            "minutes_ago": i,
            "count": buckets.get(i, 0),
        })

    return {"timeline": timeline, "bucket_size_seconds": bucket_size, "window_seconds": window}


@app.get("/api/export/{incident_id}")
async def export_incident(incident_id: str):
    """Generate exportable incident report data."""
    report = None
    for r in reports_store:
        if r.get("incident_id") == incident_id:
            report = r
            break

    if not report:
        return {"error": "Incident not found"}, 404

    # Find related anomalies
    related = [
        a for a in anomalies_store
        if a.get("icao24") in report.get("aircraft_ids", [])
        and a.get("_expires", 0) > time.time()
    ]

    return {
        "incident": report,
        "related_anomalies": related[:50],
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": (
            "This is an automated incident summary for situational awareness. "
            "All recommended actions are advisory only and require human confirmation."
        ),
    }


# ── Static files ──────────────────────────────────────────────────────

static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(static_dir, "index.html"))


# ── Startup ───────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info("Starting Ghost Track polling loop...")
    asyncio.create_task(polling_loop())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8765, reload=True)
