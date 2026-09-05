"""Early classification with temporal split — no look-ahead."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from orderbook_analyse.l2_wall_attack_discovery import DECISION_CUTOFFS_S
from orderbook_analyse.l2_wall_attack_discovery.models import safe_float

MIN_TRAIN = 30
MIN_TEST = 10


def _rule_predict(row: dict[str, Any]) -> str:
    """Transparent single-feature heuristic (frozen a priori)."""
    if row.get("pull_proxy"):
        return "PULLED_ON_CONTACT"
    refill = safe_float(row.get("refill_ratio"))
    resili = safe_float(row.get("resilience_ratio"))
    t2d = safe_float(row.get("trade_to_display_ratio"))
    prn = safe_float(row.get("price_response_per_notional"))
    attack_n = safe_float(row.get("attack_notional")) or 0.0
    if attack_n <= 0:
        return "FLOW_DIED_NO_DEFENSE"
    if resili is not None and resili >= 0.7 and (prn is None or prn < 1e-5):
        if refill is not None and refill >= 0.4:
            return "ABSORBED_REFILLED"
        return "DEFENDED"
    if t2d is not None and t2d > 1.0 and resili is not None and resili < 0.4:
        return "CLEAN_BREAK_CONTINUATION"
    if resili is not None and resili < 0.3 and (t2d is None or t2d < 0.4):
        return "PULLED_ON_CONTACT"
    return "AMBIGUOUS"


def early_classification(
    contact_rows: list[dict[str, Any]],
    labels_60: dict[str, str],
    episodes: list[dict[str, Any]],
    *,
    split_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Train = contact before split_ms; test = after. Same attack never both."""
    ep_by = {e["attack_id"]: e for e in episodes}
    metrics: list[dict[str, Any]] = []
    confusion: list[dict[str, Any]] = []

    for cut in DECISION_CUTOFFS_S:
        rows = [r for r in contact_rows if int(r["decision_cutoff_s"]) == cut and r["attack_id"] in labels_60]
        train, test = [], []
        for r in rows:
            ep = ep_by.get(r["attack_id"])
            if not ep or not ep.get("first_contact_at"):
                continue
            if int(ep["first_contact_at"]) < split_ms:
                train.append(r)
            else:
                test.append(r)
        if len(train) < MIN_TRAIN or len(test) < MIN_TEST:
            metrics.append(
                {
                    "decision_cutoff_s": cut,
                    "status": "INSUFFICIENT_SAMPLE",
                    "n_train": len(train),
                    "n_test": len(test),
                    "balanced_accuracy": None,
                    "macro_f1": None,
                    "baseline_accuracy": None,
                }
            )
            continue

        # baseline = majority in train
        train_y = [labels_60[r["attack_id"]] for r in train]
        maj = Counter(train_y).most_common(1)[0][0]
        preds = [_rule_predict(r) for r in test]
        truths = [labels_60[r["attack_id"]] for r in test]
        base_preds = [maj] * len(test)

        classes = sorted(set(truths) | set(preds))
        # confusion
        cm: dict[tuple[str, str], int] = defaultdict(int)
        for t, p in zip(truths, preds):
            cm[(t, p)] += 1
            confusion.append(
                {
                    "decision_cutoff_s": cut,
                    "true_class": t,
                    "pred_class": p,
                    "count": 1,
                }
            )

        # per-class recall then balanced acc
        recalls = []
        f1s = []
        for c in classes:
            tp = sum(1 for t, p in zip(truths, preds) if t == c and p == c)
            fn = sum(1 for t, p in zip(truths, preds) if t == c and p != c)
            fp = sum(1 for t, p in zip(truths, preds) if t != c and p == c)
            rec = tp / (tp + fn) if (tp + fn) else 0.0
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            recalls.append(rec)
            f1s.append(0.0 if (prec + rec) == 0 else 2 * prec * rec / (prec + rec))
        bal = sum(recalls) / len(recalls) if recalls else None
        macro_f1 = sum(f1s) / len(f1s) if f1s else None
        acc = sum(1 for t, p in zip(truths, preds) if t == p) / len(truths)
        base_acc = sum(1 for t, p in zip(truths, base_preds) if t == p) / len(truths)
        metrics.append(
            {
                "decision_cutoff_s": cut,
                "status": "OK",
                "n_train": len(train),
                "n_test": len(test),
                "baseline_class": maj,
                "baseline_accuracy": base_acc,
                "accuracy": acc,
                "balanced_accuracy": bal,
                "macro_f1": macro_f1,
                "model": "transparent_rule_v1",
            }
        )
    # collapse confusion duplicates
    collapsed: dict[tuple[int, str, str], int] = defaultdict(int)
    for row in confusion:
        collapsed[(row["decision_cutoff_s"], row["true_class"], row["pred_class"])] += 1
    confusion_out = [
        {"decision_cutoff_s": k[0], "true_class": k[1], "pred_class": k[2], "count": v}
        for k, v in sorted(collapsed.items())
    ]
    return metrics, confusion_out
