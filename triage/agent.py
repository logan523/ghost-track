"""Triage agent — LLM-powered alert correlation and summarization.

The agent ingests raw flagged anomalies from the detection layer and:
1. Clusters/deduplicates related flags
2. Cross-references GPSJam context
3. Scores severity (1–5)
4. Writes natural-language incident summaries

Uses DeepSeek API (OpenAI-compatible) with a rule-based fallback so the
pipeline works without an API key.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from config import config
from detector.models import AnomalyFlag, TriageReport
from detector.gpsjam import get_gpsjam_context, classify_interference
from detector.weather import get_weather_prompt_fragment, get_weather_context
from detector.cross_validate import enrich_anomaly_with_cross_check
from triage.clustering import cluster_anomalies
from triage.context import build_context_prompt, calibrate_severity, get_region_context

logger = logging.getLogger(__name__)


class TriageAgent:
    """LLM agent for anomaly triage, correlation, and summarization."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or config.deepseek_api_key
        self.model = model or config.triage_model
        self.base_url = config.deepseek_base_url
        self._client = None

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None and self.has_api_key:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    def _build_prompt(self, cluster: list[AnomalyFlag], cluster_id: str) -> str:
        """Build the structured prompt for the triage agent."""
        region = cluster[0].region if cluster else "unknown"
        context = build_context_prompt(region)

        # GPSJam positional context from first anomaly
        first = cluster[0]
        gpsjam_context = get_gpsjam_context(first.latitude, first.longitude)

        # Weather context from first anomaly's position and altitude
        weather_context = get_weather_prompt_fragment(first.latitude, first.longitude, first.altitude)

        # Cross-check enrichment for up to 5 anomalies in the cluster
        cross_check_notes = []
        for a in cluster[:5]:
            enriched = enrich_anomaly_with_cross_check({
                "icao24": a.icao24,
                "latitude": a.latitude,
                "longitude": a.longitude,
            })
            cc = enriched.get("cross_check")
            if cc and cc.get("conclusion") not in (None, "cross_check_unavailable", "aircraft_not_seen"):
                cross_check_notes.append(
                    f"  Cross-check [{a.icao24}]: {cc['conclusion']} (confidence: {cc['confidence']}) — {cc['note']}"
                )

        anomaly_lines = []
        for i, a in enumerate(cluster[:20]):  # cap at 20 for prompt length
            anomaly_lines.append(
                f"  [{i+1}] Aircraft {a.icao24} ({a.callsign or 'no callsign'}) "
                f"at {a.time.isoformat()} — "
                f"pos=({a.latitude:.4f}, {a.longitude:.4f}), alt={a.altitude:.0f}m, "
                f"flag_type={a.flag_type}, mahalanobis={a.mahalanobis_distance:.2f}, "
                f"cusum={a.cusum_score:.2f}, raw_severity={a.severity:.2f}"
            )

        cross_check_block = ""
        if cross_check_notes:
            cross_check_block = (
                "\nCross-check results (airplanes.live second-source corroboration):\n"
                + "\n".join(cross_check_notes)
                + "\n"
            )

        return f"""You are an aviation security triage analyst. Analyze the following cluster of ADS-B anomaly flags and produce a structured incident report.

{context}

{gpsjam_context}

{weather_context}
{cross_check_block}
Cluster ID: {cluster_id}
Total anomalies in cluster: {len(cluster)}
Aircraft involved: {len(set(a.icao24 for a in cluster))}
Time range: {cluster[0].time.isoformat()} to {cluster[-1].time.isoformat()}

Anomaly details:
{chr(10).join(anomaly_lines)}

Respond with a JSON object containing:
1. "severity_score": 1–5 integer (1=routine, 5=critical threat)
2. "summary": 2–3 sentence natural-language incident summary. Only state facts supported by the anomaly data above. Do not speculate about intent, actors, or causes not in evidence.
3. "recommended_action": One sentence recommending a human action (investigate, cross-check, monitor, dismiss as baseline noise). This is a RECOMMENDATION, not an action — it stays human-tasked.
4. "claims": List of specific, verifiable factual claims made in the summary (one per sentence clause). Each claim must be directly traceable to an anomaly data point above.

Respond with ONLY the JSON object, no preamble."""

    def _call_llm(self, prompt: str) -> dict:
        """Call DeepSeek API or return a rule-based fallback."""
        client = self._get_client()

        if client is not None:
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    max_tokens=1024,
                    temperature=0.0,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = response.choices[0].message.content
                return _extract_json(text)
            except Exception as e:
                logger.error(f"DeepSeek API call failed: {e}, falling back to rules")
                return self._rule_based_triage(prompt)

        logger.info("No DeepSeek API key — using rule-based triage fallback")
        return self._rule_based_triage(prompt)

    def _rule_based_triage(self, prompt: str) -> dict:
        """Deterministic rule-based triage when LLM is unavailable."""
        import re

        count_match = re.search(r"Total anomalies in cluster: (\d+)", prompt)
        count = int(count_match.group(1)) if count_match else 1

        mahal_scores = re.findall(r"mahalanobis=([\d.]+)", prompt)
        cusum_scores = re.findall(r"cusum=([\d.]+)", prompt)
        severities = re.findall(r"raw_severity=([\d.]+)", prompt)

        max_mahal = max(float(m) for m in mahal_scores) if mahal_scores else 0.0
        max_cusum = max(float(c) for c in cusum_scores) if cusum_scores else 0.0
        avg_severity = (
            sum(float(s) for s in severities) / len(severities) if severities else 0.0
        )

        region_match = re.search(r"Region: (\w+)", prompt)
        region = region_match.group(1) if region_match else "unknown"
        ctx = get_region_context(region)

        base = avg_severity * 5.0
        if max_cusum > 3.0:
            base += 1.0
        if max_mahal > 6.0:
            base += 1.0
        if count > 10:
            base += 0.5

        severity = max(1, min(5, round(base)))

        claims = [
            f"Cluster contains {count} anomaly flags",
            f"Maximum Mahalanobis distance: {max_mahal:.1f}",
            f"Maximum CUSUM score: {max_cusum:.1f}",
            f"Region: {region} (baseline: {ctx['base_jamming_level']})",
        ]

        summary = (
            f"Detected {count} ADS-B anomalies in the {region} region "
            f"(max Mahalanobis distance {max_mahal:.1f}, max CUSUM {max_cusum:.1f}). "
            f"This region has a {ctx['base_jamming_level']} baseline jamming level, "
            f"suggesting {'this is consistent with expected interference patterns' if ctx['base_jamming_level'] == 'high' else 'this may warrant further investigation'}."
        )

        action = (
            "Monitor and cross-reference with GPSJam.org for corroboration"
            if severity <= 2
            else "Cross-check against second ADS-B source and GPSJam.org before escalating"
            if severity <= 3
            else "Escalate for manual review — anomaly magnitude exceeds regional baseline"
        )

        return {
            "severity_score": severity,
            "summary": summary,
            "recommended_action": action,
            "claims": claims,
        }

    def triage(
        self, anomalies: list[AnomalyFlag]
    ) -> list[TriageReport]:
        """Full triage pipeline: cluster anomalies, then generate reports."""
        if not anomalies:
            return []

        clusters = cluster_anomalies(anomalies)

        reports = []
        for cluster in clusters:
            if not cluster:
                continue

            cluster_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
            prompt = self._build_prompt(cluster, cluster_id)
            response = self._call_llm(prompt)

            region = cluster[0].region if cluster else "unknown"
            raw_sev = response.get("severity_score", 1)
            calibrated_sev = max(1, min(5, round(raw_sev)))

            sorted_cluster = sorted(cluster, key=lambda a: a.time)
            first = sorted_cluster[0]

            # Build dynamic cross-references from all context sources
            cross_refs = [f"GPSJam.org — {region} baseline"]

            interference = classify_interference(first.latitude, first.longitude)
            if interference["zone"]:
                cross_refs.append(f"Interference zone: {interference['zone']} ({interference['level']})")

            weather = get_weather_context(first.latitude, first.longitude, first.altitude)
            if weather and weather.get("relevant_sigmets"):
                hazards = {s["hazard"] for s in weather["relevant_sigmets"]}
                cross_refs.append(f"Active SIGMETs: {', '.join(sorted(hazards))}")

            report = TriageReport(
                incident_id=cluster_id,
                aircraft_ids=list(set(a.icao24 for a in cluster)),
                time_start=sorted_cluster[0].time,
                time_end=sorted_cluster[-1].time,
                region=region,
                anomaly_count=len(cluster),
                severity_score=calibrated_sev,
                summary=response.get("summary", "No summary available"),
                recommended_action=response.get(
                    "recommended_action", "Manual review recommended"
                ),
                cross_references=cross_refs,
                claims=response.get("claims", []),
            )
            reports.append(report)

        logger.info(
            f"Triage complete: {len(anomalies)} anomalies → "
            f"{len(clusters)} clusters → {len(reports)} reports"
        )
        return reports


def _extract_json(text: str) -> dict:
    """Extract JSON object from LLM response text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    import re

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning(f"Could not parse JSON from LLM response: {text[:200]}")
    return {
        "severity_score": 1,
        "summary": "Unable to parse LLM response",
        "recommended_action": "Manual review required",
        "claims": [],
    }
