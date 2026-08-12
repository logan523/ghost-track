"""Binary metrics for synthetic labeled suite."""

from __future__ import annotations

from orbital.models import EvalMetrics


def binary_metrics(
    predicted_positive: list[bool],
    labels_anomalous: list[bool],
) -> dict:
    """predicted_positive[i] vs labels_anomalous[i]."""
    if len(predicted_positive) != len(labels_anomalous):
        raise ValueError("length mismatch")
    tp = fp = fn = tn = 0
    for pred, lab in zip(predicted_positive, labels_anomalous):
        if pred and lab:
            tp += 1
        elif pred and not lab:
            fp += 1
        elif not pred and lab:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )
    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def to_eval_metrics(
    m: dict,
    n_clean: int,
    n_anomalous: int,
    separation_ratio: float = 0.0,
    dv_boundary_m_s: float = 0.0,
) -> EvalMetrics:
    return EvalMetrics(
        precision=m["precision"],
        recall=m["recall"],
        f1=m["f1"],
        true_positives=m["true_positives"],
        false_positives=m["false_positives"],
        false_negatives=m["false_negatives"],
        n_clean=n_clean,
        n_anomalous=n_anomalous,
        separation_ratio=separation_ratio,
        dv_boundary_m_s=dv_boundary_m_s,
    )
