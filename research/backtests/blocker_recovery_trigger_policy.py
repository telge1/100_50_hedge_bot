"""Research-only catalog + helpers for the blocker recovery trigger/hybrid audit.

Defines C0..C5 variant freeze configs under **terminal stop after recovered flat**
(B1 re-entry semantics). Pure helpers only — no live config / runtime changes.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, replace
from typing import Any

from .inventory_mtm_freeze import InventoryMtmFreezeConfig
from .recovery_reentry_policy import RecoveryReentryConfig

# Prior B1 recovered / unrecovered cohorts (strict trigger+flat of original blocker).
PRIOR_B1_RECOVERED_COINS: frozenset[str] = frozenset(
    {
        "APTUSDT",
        "AVAXUSDT",
        "ARBUSDT",
        "OPUSDT",
        "LINKUSDT",
        "ADAUSDT",
        "DOTUSDT",
        "SUIUSDT",
        "SEIUSDT",
        "WLDUSDT",
        "BCHUSDT",
        "FILUSDT",
    }
)
PRIOR_B1_UNRECOVERED_COINS: frozenset[str] = frozenset(
    {
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "DOGEUSDT",
        "XRPUSDT",
        "ATOMUSDT",
        "NEARUSDT",
        "TIAUSDT",
        "INJUSDT",
        "RENDERUSDT",
        "AAVEUSDT",
        "UNIUSDT",
        "LTCUSDT",
        "ETCUSDT",
        "TRXUSDT",
    }
)

A0_SERIES_MTM = -291.96557591506945
C0_B1_REFERENCE_TRADES = 301
C0_B1_REFERENCE_CLOSED = 286
C0_B1_REFERENCE_BLOCKERS = 15
C0_B1_REFERENCE_SERIES_MTM = -168.34001076583922
C0_B1_REFERENCE_RECOVERED = 12
C0_MTM_TOLERANCE = 1.0


@dataclass(frozen=True)
class HybridVariantSpec:
    name: str
    description: str
    freeze_config: InventoryMtmFreezeConfig
    family: str  # C0 | C1 | C2 | C3 | C4 | C5


def _a1(**kwargs: Any) -> InventoryMtmFreezeConfig:
    return InventoryMtmFreezeConfig(variant="A1", **kwargs)


def _staged_a2(**kwargs: Any) -> InventoryMtmFreezeConfig:
    """A2 stage-1 exposure freeze; cycle freeze only after secondary gate."""
    defaults: dict[str, Any] = {
        "variant": "A2",
        "staged_cycle_freeze": True,
        # Disable B5's OR-bag unless a C4 variant explicitly re-enables them.
        "secondary_use_hold": False,
        "secondary_use_mtm": False,
        "secondary_use_exit_increase": False,
        "secondary_use_cycle": False,
    }
    defaults.update(kwargs)
    return InventoryMtmFreezeConfig(**defaults)


def build_c0_c4_specs() -> list[HybridVariantSpec]:
    """Fixed directed sweep (not an optimizer). All use terminal-stop via B1."""
    return [
        HybridVariantSpec(
            name="C0",
            description="B1 parity: A1 freeze at inventory_mtm < -1, terminal stop after flat",
            family="C0",
            freeze_config=_a1(threshold_usdt=-1.0),
        ),
        # C1 — earlier / later MTM thresholds
        HybridVariantSpec(
            name="C1a",
            description="A1 at inventory_mtm < -0.50",
            family="C1",
            freeze_config=_a1(threshold_usdt=-0.50),
        ),
        HybridVariantSpec(
            name="C1b",
            description="A1 at inventory_mtm < -0.75",
            family="C1",
            freeze_config=_a1(threshold_usdt=-0.75),
        ),
        HybridVariantSpec(
            name="C1c",
            description="A1 at inventory_mtm < -1.00 (same threshold as C0)",
            family="C1",
            freeze_config=_a1(threshold_usdt=-1.00),
        ),
        HybridVariantSpec(
            name="C1d",
            description="A1 at inventory_mtm < -1.25",
            family="C1",
            freeze_config=_a1(threshold_usdt=-1.25),
        ),
        HybridVariantSpec(
            name="C1e",
            description="A1 at inventory_mtm < -1.50",
            family="C1",
            freeze_config=_a1(threshold_usdt=-1.50),
        ),
        # C2 — cycle-count freeze
        HybridVariantSpec(
            name="C2a",
            description="A1 freeze at cycle_count >= 2",
            family="C2",
            freeze_config=_a1(
                use_mtm_trigger=False,
                use_cycle_trigger=True,
                cycle_count_threshold=2,
            ),
        ),
        HybridVariantSpec(
            name="C2b",
            description="A1 freeze at cycle_count >= 3",
            family="C2",
            freeze_config=_a1(
                use_mtm_trigger=False,
                use_cycle_trigger=True,
                cycle_count_threshold=3,
            ),
        ),
        HybridVariantSpec(
            name="C2c",
            description="A1 freeze at cycle_count >= 4",
            family="C2",
            freeze_config=_a1(
                use_mtm_trigger=False,
                use_cycle_trigger=True,
                cycle_count_threshold=4,
            ),
        ),
        # C3 — combined triggers
        HybridVariantSpec(
            name="C3a",
            description="A1 when inventory_mtm < -0.75 AND cycle_count >= 2",
            family="C3",
            freeze_config=_a1(
                threshold_usdt=-0.75,
                use_mtm_trigger=True,
                use_cycle_trigger=True,
                cycle_count_threshold=2,
                trigger_combine="and",
            ),
        ),
        HybridVariantSpec(
            name="C3b",
            description="A1 when inventory_mtm < -0.75 AND exit_increase_count >= 2",
            family="C3",
            freeze_config=_a1(
                threshold_usdt=-0.75,
                use_mtm_trigger=True,
                use_exit_increase_trigger=True,
                exit_increase_count_threshold=2,
                trigger_combine="and",
            ),
        ),
        HybridVariantSpec(
            name="C3c",
            description="A1 when cycle_count >= 2 AND exit_increase_count >= 2",
            family="C3",
            freeze_config=_a1(
                use_mtm_trigger=False,
                use_cycle_trigger=True,
                use_exit_increase_trigger=True,
                cycle_count_threshold=2,
                exit_increase_count_threshold=2,
                trigger_combine="and",
            ),
        ),
        HybridVariantSpec(
            name="C3d",
            description="A1 when inventory_mtm < -0.75 AND required_recovery_move_pct >= 1.0",
            family="C3",
            freeze_config=_a1(
                threshold_usdt=-0.75,
                use_mtm_trigger=True,
                use_required_recovery_move_trigger=True,
                required_recovery_move_pct_threshold=1.0,
                trigger_combine="and",
            ),
        ),
        HybridVariantSpec(
            name="C3e",
            description="A1 when inventory_mtm < -0.75 OR cycle_count >= 3",
            family="C3",
            freeze_config=_a1(
                threshold_usdt=-0.75,
                use_mtm_trigger=True,
                use_cycle_trigger=True,
                cycle_count_threshold=3,
                trigger_combine="or",
            ),
        ),
        # C4 — exposure freeze then cycle freeze
        HybridVariantSpec(
            name="C4a",
            description="A2 at mtm<-0.50, then A1 at mtm<-1.00",
            family="C4",
            freeze_config=_staged_a2(
                threshold_usdt=-0.50,
                secondary_use_mtm=True,
                secondary_mtm_threshold_usdt=-1.00,
            ),
        ),
        HybridVariantSpec(
            name="C4b",
            description="A2 at cycle_count>=2, then A1 at mtm<-1.00",
            family="C4",
            freeze_config=_staged_a2(
                use_mtm_trigger=False,
                use_cycle_trigger=True,
                cycle_count_threshold=2,
                threshold_usdt=-1.00,  # used for hold-counter bookkeeping only
                secondary_use_mtm=True,
                secondary_mtm_threshold_usdt=-1.00,
            ),
        ),
        HybridVariantSpec(
            name="C4c",
            description="A2 at exit_increase_count>=2, then A1 at cycle_count>=3",
            family="C4",
            freeze_config=_staged_a2(
                use_mtm_trigger=False,
                use_exit_increase_trigger=True,
                exit_increase_count_threshold=2,
                secondary_use_cycle=True,
                secondary_cycle_count=3,
            ),
        ),
        HybridVariantSpec(
            name="C4d",
            description="A2 at mtm<-0.75, then A1 after 100 candles still below threshold",
            family="C4",
            freeze_config=_staged_a2(
                threshold_usdt=-0.75,
                secondary_use_hold=True,
                secondary_hold_candles_below_threshold=100,
            ),
        ),
    ]


def build_c5_specs(base: HybridVariantSpec) -> list[HybridVariantSpec]:
    """Emergency neutralization variants layered on the best C0–C4 candidate."""
    windows = (250, 500)
    fractions = (0.25, 0.50)
    names = ("C5a", "C5b", "C5c", "C5d")
    specs: list[HybridVariantSpec] = []
    idx = 0
    for window in windows:
        for fraction in fractions:
            name = names[idx]
            idx += 1
            emergency_cfg = replace(
                base.freeze_config,
                emergency_neutralize_after_candles=window,
                emergency_neutralize_fraction=fraction,
            )
            specs.append(
                HybridVariantSpec(
                    name=name,
                    description=(
                        f"Emergency on top of {base.name}: neutralize {int(fraction * 100)}% "
                        f"net exposure after {window} candles without flat"
                    ),
                    family="C5",
                    freeze_config=emergency_cfg,
                )
            )
    return specs


def terminal_recovery_config(*, target_blocker_trade_number: int) -> RecoveryReentryConfig:
    """All C-variants share B1 terminal-stop semantics after recovered flat."""
    return RecoveryReentryConfig(
        variant="B1",
        target_blocker_trade_number=int(target_blocker_trade_number),
    )


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(float(v) for v in values)
    if q <= 0:
        return ordered[0]
    if q >= 1:
        return ordered[-1]
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    weight = pos - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def group_feature_stats(rows: list[dict[str, Any]], *, feature: str) -> dict[str, Any]:
    values = []
    for row in rows:
        raw = row.get(feature)
        if raw is None or raw == "":
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    if not values:
        return {
            "feature": feature,
            "n": 0,
            "mean": None,
            "median": None,
            "p25": None,
            "p75": None,
            "min": None,
            "max": None,
        }
    return {
        "feature": feature,
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p25": quantile(values, 0.25),
        "p75": quantile(values, 0.75),
        "min": min(values),
        "max": max(values),
    }


FEATURE_COLUMNS: tuple[str, ...] = (
    "trigger_candle",
    "trigger_inventory_mtm",
    "cycle_count_at_trigger",
    "exit_increase_count_at_trigger",
    "long_qty_at_trigger",
    "short_qty_at_trigger",
    "net_qty_at_trigger",
    "gross_notional_at_trigger",
    "net_exposure_usdt_at_trigger",
    "exit_distance_pct_at_trigger",
    "required_recovery_move_pct_at_trigger",
    "realized_cycle_pnl_at_trigger",
    "pending_cycle_loss_at_trigger",
    "worst_mtm_after_trigger",
    "maximum_adverse_price_move_after_trigger",
    "maximum_favorable_price_move_after_trigger",
    "trigger_to_flat_candles",
    "final_mtm",
)


def rank_separating_features(
    recovered_rows: list[dict[str, Any]],
    unrecovered_rows: list[dict[str, Any]],
    *,
    features: tuple[str, ...] = FEATURE_COLUMNS,
) -> list[dict[str, Any]]:
    """Rank features by absolute standardized mean difference (diagnostic only)."""
    ranked: list[dict[str, Any]] = []
    for feature in features:
        rec = group_feature_stats(recovered_rows, feature=feature)
        unrec = group_feature_stats(unrecovered_rows, feature=feature)
        if not rec["n"] or not unrec["n"] or rec["mean"] is None or unrec["mean"] is None:
            continue
        rec_vals = [float(r[feature]) for r in recovered_rows if r.get(feature) not in (None, "")]
        unrec_vals = [float(r[feature]) for r in unrecovered_rows if r.get(feature) not in (None, "")]
        pooled = rec_vals + unrec_vals
        if len(pooled) < 2:
            continue
        std = statistics.pstdev(pooled)
        if std <= 1e-12:
            effect = 0.0
        else:
            effect = (float(rec["mean"]) - float(unrec["mean"])) / std
        ranked.append(
            {
                "feature": feature,
                "recovered_mean": rec["mean"],
                "unrecovered_mean": unrec["mean"],
                "recovered_median": rec["median"],
                "unrecovered_median": unrec["median"],
                "abs_std_mean_diff": abs(effect),
                "std_mean_diff": effect,
            }
        )
    ranked.sort(key=lambda row: float(row["abs_std_mean_diff"]), reverse=True)
    return ranked


def pick_best_c0_c4_candidate(variant_summaries: list[dict[str, Any]]) -> str:
    """Best terminal series_mtm among C0–C4; ties broken by recovery_rate then name."""
    eligible = [
        row
        for row in variant_summaries
        if str(row.get("variant", "")).startswith(("C0", "C1", "C2", "C3", "C4"))
        and not str(row.get("variant", "")).startswith("C5")
    ]
    if not eligible:
        return "C0"

    def key(row: dict[str, Any]) -> tuple[float, float, str]:
        return (
            float(row.get("series_mtm_terminal_stop") or row.get("series_mtm") or float("-inf")),
            float(row.get("recovery_rate") or 0.0),
            str(row.get("variant") or ""),
        )

    return str(max(eligible, key=key)["variant"])
