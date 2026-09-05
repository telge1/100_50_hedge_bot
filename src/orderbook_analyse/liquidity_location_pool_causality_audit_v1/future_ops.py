"""Static future-operator classification for the LLD pool call path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .config import DASHBOARD_ROOT, ENGINE_ROOT

SAFE = "SAFE"
SAFE_DELAYED = "SAFE_WITH_DELAYED_KNOWN_AT"
LOOKAHEAD = "LOOKAHEAD"
REPAINT = "REPAINT_RISK"
NOT_USED = "NOT_USED_IN_POOL_PATH"


def build_future_operator_audit() -> list[dict[str, Any]]:
    """Hand-audited findings grounded in source inspection (not auto-guessed)."""
    rows: list[dict[str, Any]] = [
        {
            "location": "TRP volume_strength.percentile_nearest_rank",
            "pattern": "rolling percentile length=1000",
            "classification": SAFE,
            "notes": "Causal expanding/sliding window using only bars <= i; warmup na until 1000 finite samples. Not full-run quantile.",
        },
        {
            "location": "TRP volume_strength.rolling_highest/lowest",
            "pattern": "window [i-length+1, i]",
            "classification": SAFE,
            "notes": "Lookback only; no center=True.",
        },
        {
            "location": "TRP engine._run_pools",
            "pattern": "source=candles[i-1], confirm=candles[i]",
            "classification": SAFE_DELAYED,
            "notes": (
                "Geometry/strength from prior closed TF bar; confirmation uses full OHLC of candle i. "
                "created_timestamp stamped as candle-i OPEN. Causal availability is candle-i CLOSE. "
                "If known_at is treated as availability, this is LOOKAHEAD by one TF period."
            ),
        },
        {
            "location": "TRP engine._run_pools",
            "pattern": "negative shift / shift(-1)",
            "classification": NOT_USED,
            "notes": "No pandas shift(-1); uses explicit i-1 indexing only.",
        },
        {
            "location": "TRP engine / volume_strength",
            "pattern": "rolling(..., center=True)",
            "classification": NOT_USED,
            "notes": "No centered windows in pool path.",
        },
        {
            "location": "TRP engine / volume_strength",
            "pattern": "bfill / forward-fill from future",
            "classification": NOT_USED,
            "notes": "No bfill/ffill of future series into birth fields.",
        },
        {
            "location": "TRP engine._select_displayed amount prune",
            "pattern": "keep newest amount boxes",
            "classification": SAFE,
            "notes": "Display-only on result.pools; scanner/audit use pools_all so birth set not pruned.",
        },
        {
            "location": "TRP clusters.cluster_pools(as_of=T)",
            "pattern": "membership filter created<=T and not invalidated",
            "classification": SAFE,
            "notes": "Later members excluded from earlier as_of. Cluster id = hash(members) so membership change yields new id (implicit versioning).",
        },
        {
            "location": "TRP clusters._build_cluster edges",
            "pattern": "min/max of member edges at as_of",
            "classification": SAFE,
            "notes": "At fixed as_of, edges use only members known at T. Different as_of can widen a price region under a NEW cluster_id.",
        },
        {
            "location": "clickhouse_source.aggregate_timeframe",
            "pattern": "resample label=left closed=left; drop incomplete last",
            "classification": SAFE,
            "notes": "Correct closed-bucket semantics when aggregation input is prefix-truncated to bar_end<=T.",
        },
        {
            "location": "a_plus.pools.load_pools_at",
            "pattern": "hist = df[open_time <= as_of] on pre-aggregated full HTF",
            "classification": LOOKAHEAD,
            "notes": (
                "When candles_by_tf is built once from the full 1m window, HTF rows with open_time<=as_of "
                "may already embed 1m OHLC after as_of (incomplete-at-T bars completed by future minutes). "
                "Proven on DOGE 15m ref pool: scanner-style shows pool at 03:30; causal prefix only at 03:45."
            ),
        },
        {
            "location": "a_plus.runner.build_candles_by_tf",
            "pattern": "aggregate_timeframe(full 1m range) then scan",
            "classification": LOOKAHEAD,
            "notes": "Root cause enabling load_pools_at HTF leakage; fix requires per-as_of re-aggregation from closed 1m prefix.",
        },
        {
            "location": "PoolRecord.known_at / chart compose start_timestamp",
            "pattern": "known_at = created_timestamp = confirmation OPEN",
            "classification": LOOKAHEAD,
            "notes": (
                "Chart zone starts at confirmation open while confirmation OHLC requires the full TF bar. "
                "Overlay therefore claims earlier visibility than closed-bar causality allows unless the "
                "consumer only draws after confirmation close."
            ),
        },
        {
            "location": "dashboard research_charts.service.apply_live_forming_tip",
            "pattern": "mutates last open candle",
            "classification": REPAINT,
            "notes": "Live tip can mutate forming HTF candle; research audit path uses closed aggregates only. Live charts are REPAINT_RISK for tip path.",
        },
        {
            "location": "a_plus.scanner pending-plan invalidation via snapshot absence",
            "pattern": "pool_present_in_snapshot False → INVALIDATED_UNFILLED",
            "classification": REPAINT,
            "notes": (
                "Absence conflates true invalidation with technical non-presence (lookahead correction, "
                "HTF leakage flip, cluster id change). Explicit invalidated_at required for reliable lifecycle."
            ),
        },
        {
            "location": "strength / component_count fields",
            "pattern": "single strength field on LiquidityPool",
            "classification": SAFE,
            "notes": "Strength set at creation from norm_vol[i-1]; not overwritten later. No separate strength_current; invalidation only flips active/invalidated_timestamp.",
        },
        {
            "location": "global quantile over analysis window",
            "pattern": "full-run ranking",
            "classification": NOT_USED,
            "notes": "Birth strength uses causal rolling 99th percentile only.",
        },
    ]
    # File presence checks (paths for methodology)
    for p in [
        ENGINE_ROOT / "indicators/liquidity_location/engine.py",
        ENGINE_ROOT / "indicators/liquidity_location/volume_strength.py",
        ENGINE_ROOT / "indicators/liquidity_location/clusters.py",
        ENGINE_ROOT / "indicators/liquidity_location/compose.py",
        DASHBOARD_ROOT / "research_charts/service.py",
    ]:
        rows.append(
            {
                "location": str(p),
                "pattern": "path_exists",
                "classification": SAFE if p.exists() else NOT_USED,
                "notes": "exists" if p.exists() else "missing",
            }
        )
    return rows


def write_future_operator_csv(path: Path) -> pd.DataFrame:
    df = pd.DataFrame(build_future_operator_audit())
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df
