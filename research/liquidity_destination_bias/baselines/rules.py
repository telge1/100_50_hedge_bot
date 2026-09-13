"""Transparent deterministic baseline decision rules."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Callable

from .contract import MAJORITY_TIE_BREAK, PRIMARY_CLASSES
from .data import EpisodeRow


Predictor = Callable[[EpisodeRow], str]


def majority_label(rows: list[EpisodeRow]) -> str:
    """Choose most frequent primary class; ties use frozen NEITHER>UPPER>LOWER."""
    counts = Counter(r.outcome for r in rows if r.outcome in PRIMARY_CLASSES)
    if not counts:
        return MAJORITY_TIE_BREAK[0]
    best = max(counts.values())
    tied = [cls for cls, n in counts.items() if n == best]
    for cls in MAJORITY_TIE_BREAK:
        if cls in tied:
            return cls
    return tied[0]


def nearest_target(row: EpisodeRow) -> str:
    if row.distance_upper_bps < row.distance_lower_bps:
        return "UPPER_FIRST"
    if row.distance_lower_bps < row.distance_upper_bps:
        return "LOWER_FIRST"
    return "NEITHER_WITHIN_HORIZON"


def inverse_nearest(row: EpisodeRow) -> str:
    if row.distance_upper_bps < row.distance_lower_bps:
        return "LOWER_FIRST"
    if row.distance_lower_bps < row.distance_upper_bps:
        return "UPPER_FIRST"
    return "NEITHER_WITHIN_HORIZON"


def hash_control(row: EpisodeRow, *, dataset_fingerprint: str) -> str:
    digest = hashlib.sha256(
        f"{dataset_fingerprint}|{row.episode_id}".encode("utf-8")
    ).digest()
    return PRIMARY_CLASSES[digest[0] % len(PRIMARY_CLASSES)]


def make_predictors(
    train_rows: list[EpisodeRow],
    *,
    dataset_fingerprint: str,
) -> dict[str, Predictor]:
    global_majority = majority_label(train_rows)
    by_symbol = {
        symbol: majority_label([r for r in train_rows if r.symbol == symbol])
        for symbol in sorted({r.symbol for r in train_rows})
    }

    def b0(_row: EpisodeRow) -> str:
        return global_majority

    def b4(row: EpisodeRow) -> str:
        return by_symbol.get(row.symbol, global_majority)

    def b3(row: EpisodeRow) -> str:
        return hash_control(row, dataset_fingerprint=dataset_fingerprint)

    return {
        "B0_TRAIN_MAJORITY": b0,
        "B1_NEAREST_TARGET": nearest_target,
        "B2_INVERSE_NEAREST": inverse_nearest,
        "B3_DETERMINISTIC_HASH_CONTROL": b3,
        "B4_SYMBOL_TRAIN_MAJORITY": b4,
    }


def fitted_labels(
    train_rows: list[EpisodeRow],
) -> dict[str, str | dict[str, str]]:
    return {
        "B0_TRAIN_MAJORITY": majority_label(train_rows),
        "B4_SYMBOL_TRAIN_MAJORITY": {
            symbol: majority_label([r for r in train_rows if r.symbol == symbol])
            for symbol in sorted({r.symbol for r in train_rows})
        },
    }
