"""Evaluation metrics for detection and triage layers.

Computes precision, recall, F1 for:
- Detection layer: per-anomaly flag classification
- Triage layer: severity scoring vs ground-truth severity labels

Includes the circularity-risk disclosure required by the spec (§6).
"""

import logging
from dataclasses import dataclass
from typing import Optional

from detector.models import AnomalyFlag, EvaluationResult, TriageReport
from eval.ground_truth import GroundTruthLabel, label_anomalies, get_disclosure_text

logger = logging.getLogger(__name__)


def evaluate_detector(
    anomalies: list[AnomalyFlag],
    ground_truth: list[GroundTruthLabel],
) -> dict:
    """Compute precision/recall for the detection layer.

    An anomaly is a true positive if it falls within a known-incident
    time window AND region. It is a false positive if it falls within
    a control period. Anomalies outside both labeled periods are excluded
    from the calculation (they're "unlabeled", not false positives).
    """
    labeled = label_anomalies(anomalies, ground_truth)

    # Only count labeled anomalies (exclude "unlabeled" from metric)
    tp = sum(
        1 for l in labeled if l["is_true_positive"] and not l["is_false_positive"]
    )
    fp = sum(1 for l in labeled if l["is_false_positive"])

    # False negatives: known-incident periods where we found NO anomalies
    # This is approximate — we count ground-truth incident periods that
    # have zero matching anomalies
    incident_periods = [gt for gt in ground_truth if gt.is_anomalous]
    fn = 0
    for gt in incident_periods:
        has_match = any(
            l["is_true_positive"]
            for l in labeled
            if hasattr(l["anomaly"], "region")
            and l["anomaly"].region == gt.region
        )
        if not has_match:
            fn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "unlabeled": sum(
            1
            for l in labeled
            if not l["is_true_positive"] and not l["is_false_positive"]
        ),
        "disclosure": get_disclosure_text(),
    }


def evaluate_triage_severity(
    reports: list[TriageReport],
    ground_truth: list[GroundTruthLabel],
) -> dict:
    """Evaluate triage severity scoring against ground-truth labels.

    A report's severity (1–5) is compared to expected severity based on
    the ground-truth region context. High-severity reports in known-incident
    periods are true positives; high-severity in control periods are false
    positives.
    """
    tp = fp = fn = 0

    for report in reports:
        matched = False
        for gt in ground_truth:
            if (
                report.region == gt.region
                and gt.time_start <= report.time_start <= gt.time_end
            ):
                if gt.is_anomalous and report.severity_score >= 3:
                    tp += 1
                elif not gt.is_anomalous and report.severity_score >= 3:
                    fp += 1
                elif gt.is_anomalous and report.severity_score < 3:
                    fn += 1
                matched = True
                break

        if not matched and report.severity_score >= 3:
            fp += 1  # unlabeled but high severity = potential false positive

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def build_evaluation_result(
    detector_metrics: dict,
    triage_metrics: Optional[dict] = None,
    hallucination_metrics: Optional[dict] = None,
    adversarial_result: Optional[dict] = None,
) -> EvaluationResult:
    """Build the full EvaluationResult from component metrics."""
    return EvaluationResult(
        detector_precision=detector_metrics["precision"],
        detector_recall=detector_metrics["recall"],
        detector_f1=detector_metrics["f1"],
        true_positives=detector_metrics["true_positives"],
        false_positives=detector_metrics["false_positives"],
        false_negatives=detector_metrics["false_negatives"],
        triage_precision=triage_metrics["precision"] if triage_metrics else 0.0,
        triage_recall=triage_metrics["recall"] if triage_metrics else 0.0,
        triage_f1=triage_metrics["f1"] if triage_metrics else 0.0,
        hallucination_rate=(
            hallucination_metrics["hallucination_rate"]
            if hallucination_metrics
            else 0.0
        ),
        total_claims_checked=(
            hallucination_metrics.get("total_claims_checked", 0)
            if hallucination_metrics
            else 0
        ),
        unverifiable_claims=(
            hallucination_metrics.get("unverifiable_claims", 0)
            if hallucination_metrics
            else 0
        ),
        drift_detection_boundary=(
            adversarial_result["boundary_m_s"] if adversarial_result else 0.0
        ),
    )
