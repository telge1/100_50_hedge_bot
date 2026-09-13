"""End-to-end Phase-2 baseline evaluation over frozen Phase-1D episodes."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .contract import (
    AMBIGUOUS_OUTCOME,
    BASELINE_IDS,
    DIRECTIONAL_CLASSES,
    EVALUATION_CONTRACT_VERSION,
    PRIMARY_CLASSES,
    RESEARCH_BANNER,
)
from .data import EpisodeRow, verify_and_load_dataset
from .metrics import classification_metrics, wilson_interval
from .rules import fitted_labels, make_predictors
from .splits import (
    assert_no_split_leak,
    class_counts,
    directional_rows,
    primary_rows,
    split_episodes,
)


def _scope_rows(rows: list[EpisodeRow], symbol: str | None) -> list[EpisodeRow]:
    if symbol is None or symbol == "COMBINED":
        return rows
    return [r for r in rows if r.symbol == symbol]


def _metric_row(
    *,
    baseline: str,
    split: str,
    scope: str,
    metrics: dict[str, Any],
    wilson: tuple[float, float] | None = None,
) -> dict[str, Any]:
    row = {
        "baseline": baseline,
        "split": split,
        "scope": scope,
        "n": metrics["n"],
        "accuracy": metrics["accuracy"],
        "balanced_accuracy": metrics["balanced_accuracy"],
        "macro_precision": metrics["macro_precision"],
        "macro_recall": metrics["macro_recall"],
        "macro_f1": metrics["macro_f1"],
        "warnings": "|".join(metrics.get("warnings") or []),
        "research_only": RESEARCH_BANNER,
    }
    if wilson is not None:
        row["wilson_accuracy_low"] = wilson[0]
        row["wilson_accuracy_high"] = wilson[1]
    return row


def evaluate_baselines(
    dataset_root: Path,
    output_dir: Path,
    *,
    require_frozen_manifest: bool = True,
) -> dict[str, Any]:
    episodes, meta = verify_and_load_dataset(
        dataset_root, require_frozen_manifest=require_frozen_manifest
    )
    splits = split_episodes(episodes)
    assert_no_split_leak(splits)
    predictors = make_predictors(
        splits["TRAIN"], dataset_fingerprint=meta["dataset_fingerprint_sha256"]
    )
    fitted = fitted_labels(splits["TRAIN"])

    scopes = ("BTCUSDT", "DOGEUSDT", "COMBINED")
    baseline_metric_rows: list[dict[str, Any]] = []
    directional_rows_out: list[dict[str, Any]] = []
    confusion: dict[str, Any] = {}
    daily_rows: list[dict[str, Any]] = []
    split_inventory: list[dict[str, Any]] = []

    ambiguous_total = sum(1 for r in episodes if r.outcome == AMBIGUOUS_OUTCOME)

    for split_name, split_rows in splits.items():
        for scope in scopes:
            scoped = _scope_rows(split_rows, None if scope == "COMBINED" else scope)
            counts = class_counts(scoped)
            split_inventory.append(
                {
                    "split": split_name,
                    "scope": scope,
                    "n_total": len(scoped),
                    "n_primary": len(primary_rows(scoped)),
                    "n_directional": len(directional_rows(scoped)),
                    "n_ambiguous": counts.get(AMBIGUOUS_OUTCOME, 0),
                    **{f"actual_{k}": v for k, v in counts.items()},
                }
            )

    for baseline in BASELINE_IDS:
        predict = predictors[baseline]
        confusion[baseline] = {}
        for split_name, split_rows in splits.items():
            confusion[baseline][split_name] = {}
            for scope in scopes:
                scoped = _scope_rows(
                    split_rows, None if scope == "COMBINED" else scope
                )
                primary = primary_rows(scoped)
                y_true = [r.outcome for r in primary]
                y_pred = [predict(r) for r in primary]
                metrics = classification_metrics(y_true, y_pred, PRIMARY_CLASSES)
                correct = sum(t == p for t, p in zip(y_true, y_pred))
                wilson = (
                    wilson_interval(correct, len(y_true))
                    if split_name == "TEST"
                    else None
                )
                baseline_metric_rows.append(
                    _metric_row(
                        baseline=baseline,
                        split=split_name,
                        scope=scope,
                        metrics=metrics,
                        wilson=wilson,
                    )
                )
                confusion[baseline][split_name][scope] = {
                    "primary": metrics["confusion_matrix"],
                    "prediction_distribution": metrics["prediction_distribution"],
                    "actual_class_distribution": metrics["actual_class_distribution"],
                    "warnings": metrics["warnings"],
                }

                direction = directional_rows(scoped)
                dy_true = [r.outcome for r in direction]
                dy_pred = [predict(r) for r in direction]
                # If predictor emits NEITHER on directional subset, keep it and
                # score against directional classes only for true labels; expand
                # class set for confusion to include NEITHER predictions.
                dir_classes = list(DIRECTIONAL_CLASSES) + ["NEITHER_WITHIN_HORIZON"]
                # Map unexpected predictions fail-closed already because rules
                # only emit primary classes.
                d_metrics = classification_metrics(dy_true, dy_pred, dir_classes)
                # Directional accuracy ignores neither predictions as incorrect
                # against true UPPER/LOWER labels.
                directional_acc = d_metrics["accuracy"]
                present = [c for c in DIRECTIONAL_CLASSES if d_metrics["actual_class_distribution"][c] > 0]
                if present:
                    bal = sum(
                        d_metrics["confusion_matrix"][c][c]
                        / d_metrics["actual_class_distribution"][c]
                        for c in present
                    ) / len(present)
                else:
                    bal = 0.0
                directional_rows_out.append(
                    {
                        "baseline": baseline,
                        "split": split_name,
                        "scope": scope,
                        "n_directional": len(direction),
                        "excluded_neither_count": len(scoped) - len(direction),
                        "directional_accuracy": directional_acc,
                        "balanced_directional_accuracy": bal,
                        "warnings": "|".join(d_metrics["warnings"]),
                        "research_only": RESEARCH_BANNER,
                    }
                )
                confusion[baseline][split_name][scope]["directional"] = (
                    d_metrics["confusion_matrix"]
                )

            # daily stability on TEST COMBINED and per symbol
            if split_name == "TEST":
                for scope in scopes:
                    scoped = _scope_rows(
                        split_rows, None if scope == "COMBINED" else scope
                    )
                    by_day: dict[str, list[EpisodeRow]] = defaultdict(list)
                    for row in primary_rows(scoped):
                        by_day[row.t0_utc.date().isoformat()].append(row)
                    day_acc = {}
                    for day, rows in sorted(by_day.items()):
                        yt = [r.outcome for r in rows]
                        yp = [predict(r) for r in rows]
                        m = classification_metrics(yt, yp, PRIMARY_CLASSES)
                        day_acc[day] = m["accuracy"]
                        daily_rows.append(
                            {
                                "baseline": baseline,
                                "scope": scope,
                                "utc_day": day,
                                "n": m["n"],
                                "accuracy": m["accuracy"],
                                "balanced_accuracy": m["balanced_accuracy"],
                                "research_only": RESEARCH_BANNER,
                            }
                        )
                    if day_acc:
                        best = max(day_acc, key=day_acc.get)
                        worst = min(day_acc, key=day_acc.get)
                        for row in daily_rows:
                            if (
                                row["baseline"] == baseline
                                and row["scope"] == scope
                                and row["utc_day"] in day_acc
                            ):
                                row["is_best_test_day"] = row["utc_day"] == best
                                row["is_worst_test_day"] = row["utc_day"] == worst

    distance_rows = _distance_diagnostics(episodes, predictors["B1_NEAREST_TARGET"])

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "split_inventory.csv", split_inventory)
    _write_csv(output_dir / "baseline_metrics.csv", baseline_metric_rows)
    _write_csv(output_dir / "directional_metrics.csv", directional_rows_out)
    _write_csv(output_dir / "daily_stability.csv", daily_rows)
    _write_csv(output_dir / "distance_diagnostics.csv", distance_rows)
    (output_dir / "confusion_matrices.json").write_text(
        json.dumps(confusion, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    summary = {
        "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
        "research_only": RESEARCH_BANNER,
        "dataset": meta,
        "fitted_train_labels": fitted,
        "ambiguous_total": ambiguous_total,
        "split_counts": {
            name: {
                "n": len(rows),
                "by_symbol": dict(Counter(r.symbol for r in rows)),
                "outcomes": class_counts(rows),
            }
            for name, rows in splits.items()
        },
        "baseline_metrics": baseline_metric_rows,
        "directional_metrics": directional_rows_out,
    }
    (output_dir / "evaluation_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def _distance_diagnostics(
    episodes: list[EpisodeRow], nearest_predict
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    primary = primary_rows(episodes)

    def bucket(rows_in: list[EpisodeRow], label: str, scope: str) -> dict[str, Any]:
        nearer_won = farther_won = neither = ties = 0
        upper_near = lower_near = 0
        ratios_by_outcome: dict[str, list[float]] = defaultdict(list)
        hour_counts = Counter()
        day_counts = Counter()
        for r in rows_in:
            up, lo = r.distance_upper_bps, r.distance_lower_bps
            if up < lo:
                upper_near += 1
                nearer = "UPPER_FIRST"
                farther = "LOWER_FIRST"
            elif lo < up:
                lower_near += 1
                nearer = "LOWER_FIRST"
                farther = "UPPER_FIRST"
            else:
                ties += 1
                nearer = farther = "NEITHER_WITHIN_HORIZON"
            if r.outcome == "NEITHER_WITHIN_HORIZON":
                neither += 1
            elif nearer != "NEITHER_WITHIN_HORIZON" and r.outcome == nearer:
                nearer_won += 1
            elif farther != "NEITHER_WITHIN_HORIZON" and r.outcome == farther:
                farther_won += 1
            if lo > 0:
                ratios_by_outcome[r.outcome].append(up / lo)
            if nearest_predict(r) == r.outcome:
                day_counts[r.t0_utc.date().isoformat()] += 1
                hour_counts[f"{r.t0_utc.hour:02d}"] += 1
        def med(vals: list[float]) -> float | None:
            if not vals:
                return None
            svals = sorted(vals)
            mid = len(svals) // 2
            if len(svals) % 2:
                return svals[mid]
            return 0.5 * (svals[mid - 1] + svals[mid])

        n = len(rows_in)
        return {
            "slice": label,
            "scope": scope,
            "n": n,
            "nearer_target_won": nearer_won,
            "farther_target_won": farther_won,
            "neither_count": neither,
            "exact_distance_ties": ties,
            "upper_nearer_count": upper_near,
            "lower_nearer_count": lower_near,
            "share_nearer_won": nearer_won / n if n else 0.0,
            "share_farther_won": farther_won / n if n else 0.0,
            "share_neither": neither / n if n else 0.0,
            "median_ratio_when_upper_first": med(ratios_by_outcome.get("UPPER_FIRST", [])),
            "median_ratio_when_lower_first": med(ratios_by_outcome.get("LOWER_FIRST", [])),
            "median_ratio_when_neither": med(
                ratios_by_outcome.get("NEITHER_WITHIN_HORIZON", [])
            ),
            "nearest_correct_top_day": (
                day_counts.most_common(1)[0][0] if day_counts else ""
            ),
            "nearest_correct_top_day_count": (
                day_counts.most_common(1)[0][1] if day_counts else 0
            ),
            "nearest_correct_top_hour": (
                hour_counts.most_common(1)[0][0] if hour_counts else ""
            ),
            "nearest_correct_top_hour_count": (
                hour_counts.most_common(1)[0][1] if hour_counts else 0
            ),
        }

    for scope_name, scoped in (
        ("COMBINED", primary),
        ("BTCUSDT", [r for r in primary if r.symbol == "BTCUSDT"]),
        ("DOGEUSDT", [r for r in primary if r.symbol == "DOGEUSDT"]),
    ):
        rows.append(bucket(scoped, "all_primary", scope_name))
        upper_near_rows = [
            r for r in scoped if r.distance_upper_bps < r.distance_lower_bps
        ]
        lower_near_rows = [
            r for r in scoped if r.distance_lower_bps < r.distance_upper_bps
        ]
        rows.append(bucket(upper_near_rows, "upper_nearer", scope_name))
        rows.append(bucket(lower_near_rows, "lower_nearer", scope_name))
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
