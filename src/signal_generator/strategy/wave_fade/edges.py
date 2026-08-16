"""Frozen APT-IS efficiency quartile edges (Tier-A Q4).

Freeze semantics (``frozen_eff_edges_all_signal_tfs``):
- Symbol for edges: APTUSDT only
- Cutoff: end_available_at <= APT_IS_END (2026-08-08T10:21:00Z)
- Per (timeframe, direction, metric): pandas quantile 0.25 / 0.5 / 0.75
- Metrics: directional_efficiency, signed_price_move_pct
- Never refit from future data or other coins

Shipped snapshot: ``data/frozen_eff_edges_apt_is.json`` — computed once via the
freeze MySQL path for APTUSDT signal TFs (verified identical to APT wave-CSV
recompute with the same cutoff).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

import pandas as pd

from signal_generator.strategy.wave_fade.parameters import (
    APT_IS_END,
    EFF_EDGE_SYMBOL,
    FROZEN_EFF_EDGES_PATH,
    SIGNAL_TFS,
)


def compute_frozen_eff_edges_from_waves(
    waves_by_tf: Mapping[str, pd.DataFrame],
    *,
    is_end: str | pd.Timestamp = APT_IS_END,
    symbol: str = EFF_EDGE_SYMBOL,
) -> dict[tuple[str, str, str], dict[float, float]]:
    """Exact freeze quartile algorithm on pre-built APT waves (no live refit)."""
    if symbol != EFF_EDGE_SYMBOL:
        raise ValueError(
            f"Frozen edges are defined only for {EFF_EDGE_SYMBOL}; got {symbol!r}"
        )
    cutoff = pd.Timestamp(is_end)
    if cutoff.tzinfo is None:
        cutoff = cutoff.tz_localize("UTC")
    else:
        cutoff = cutoff.tz_convert("UTC")

    edges: dict[tuple[str, str, str], dict[float, float]] = {}
    for tf in SIGNAL_TFS:
        w = waves_by_tf.get(tf)
        if w is None or w.empty:
            continue
        ww = w.copy()
        ww["end_available_at"] = pd.to_datetime(ww["end_available_at"], utc=True)
        ww = ww[ww["end_available_at"] <= cutoff]
        for direction, g in ww.groupby(ww["direction"].astype(str)):
            for col in ("directional_efficiency", "signed_price_move_pct"):
                s = g[col].astype(float).dropna()
                q = s.quantile([0.25, 0.5, 0.75])
                edges[(tf, str(direction), col)] = {
                    0.25: float(q.loc[0.25]),
                    0.5: float(q.loc[0.5]),
                    0.75: float(q.loc[0.75]),
                }
    return edges


def load_frozen_eff_edges(
    path: Path | None = None,
) -> dict[tuple[str, str, str], dict[float, float]]:
    """Load shipped APT-IS edges (production default — no refit)."""
    p = path or FROZEN_EFF_EDGES_PATH
    raw = json.loads(p.read_text(encoding="utf-8"))
    edges: dict[tuple[str, str, str], dict[float, float]] = {}
    for key, vals in raw["edges"].items():
        tf, direction, col = key.split("|", 2)
        edges[(tf, direction, col)] = {
            0.25: float(vals["0.25"]),
            0.5: float(vals["0.5"]),
            0.75: float(vals["0.75"]),
        }
    return edges


# Alias matching freeze name — loads shipped snapshot (does not hit MySQL).
def frozen_eff_edges_all_signal_tfs() -> dict[tuple[str, str, str], dict[float, float]]:
    return load_frozen_eff_edges()
