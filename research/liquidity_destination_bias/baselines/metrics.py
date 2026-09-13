"""Deterministic classification metrics without NaN/Infinity."""

from __future__ import annotations

import math
from collections import Counter
from typing import Iterable, Sequence


def _safe_div(num: float, den: float) -> float:
    if den == 0:
        return 0.0
    return num / den


def confusion_matrix(
    y_true: Sequence[str],
    y_pred: Sequence[str],
    classes: Sequence[str],
) -> dict[str, dict[str, int]]:
    matrix = {actual: {pred: 0 for pred in classes} for actual in classes}
    for actual, pred in zip(y_true, y_pred):
        if actual not in matrix or pred not in matrix[actual]:
            raise ValueError(f"label outside class set: actual={actual} pred={pred}")
        matrix[actual][pred] += 1
    return matrix


def classification_metrics(
    y_true: Sequence[str],
    y_pred: Sequence[str],
    classes: Sequence[str],
) -> dict:
    if len(y_true) != len(y_pred):
        raise ValueError("y_true/y_pred length mismatch")
    n = len(y_true)
    warnings: list[str] = []
    cm = confusion_matrix(y_true, y_pred, classes)
    actual_dist = {cls: sum(cm[cls].values()) for cls in classes}
    pred_dist = {
        cls: sum(cm[actual][cls] for actual in classes) for cls in classes
    }
    correct = sum(cm[cls][cls] for cls in classes)
    accuracy = _safe_div(correct, n)

    recalls = []
    precisions = []
    f1s = []
    for cls in classes:
        tp = cm[cls][cls]
        fp = pred_dist[cls] - tp
        fn = actual_dist[cls] - tp
        if actual_dist[cls] == 0:
            warnings.append(f"missing_actual_class:{cls}")
            recall = 0.0
        else:
            recall = _safe_div(tp, actual_dist[cls])
        if pred_dist[cls] == 0:
            warnings.append(f"missing_predicted_class:{cls}")
            precision = 0.0
        else:
            precision = _safe_div(tp, pred_dist[cls])
        f1 = _safe_div(2 * precision * recall, precision + recall) if (
            precision + recall
        ) else 0.0
        recalls.append(recall)
        precisions.append(precision)
        f1s.append(f1)

    present = [cls for cls in classes if actual_dist[cls] > 0]
    if present:
        balanced = sum(
            _safe_div(cm[cls][cls], actual_dist[cls]) for cls in present
        ) / len(present)
    else:
        balanced = 0.0
        warnings.append("no_actual_classes")

    out = {
        "n": n,
        "accuracy": accuracy,
        "balanced_accuracy": balanced,
        "macro_precision": sum(precisions) / len(classes) if classes else 0.0,
        "macro_recall": sum(recalls) / len(classes) if classes else 0.0,
        "macro_f1": sum(f1s) / len(classes) if classes else 0.0,
        "confusion_matrix": cm,
        "prediction_distribution": pred_dist,
        "actual_class_distribution": actual_dist,
        "warnings": warnings,
    }
    for key, value in out.items():
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise ValueError(f"non-finite metric {key}")
    return out


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt((p * (1 - p) / n) + (z2 / (4 * n * n)))
    lo = max(0.0, center - margin)
    hi = min(1.0, center + margin)
    return (lo, hi)


def finite_check(values: Iterable[float]) -> None:
    for value in values:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise ValueError("NaN/Infinity forbidden")
