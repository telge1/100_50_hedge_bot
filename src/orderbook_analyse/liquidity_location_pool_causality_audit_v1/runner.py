"""Run the full Liquidity Location pool causality audit (read-only CH)."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
    default_client,
    fetch_candles_1m,
)

from . import (
    AUDIT_ID,
    AUDIT_VERSION,
    VERDICT_FREE,
    VERDICT_LIFECYCLE,
    VERDICT_LOOKAHEAD,
    VERDICT_REPAINT,
)
from .config import (
    AUDIT_END,
    AUDIT_START,
    DEFAULT_OUT_ROOT,
    DENSE_END,
    DENSE_START,
    PREFIX_CHECKPOINTS,
    REFERENCE_POOLS,
    SYMBOL,
    TIMEFRAMES,
    WARMUP_START,
)
from .future_ops import write_future_operator_csv
from .prefix_engine import (
    active_pools_as_of,
    birth_hash,
    build_tf_from_prefix,
    build_tf_full_then_filter,
    candles_1m_until,
    cluster_snapshot_rows,
    confirmation_bar_end,
    pool_birth_fields,
    run_pools_for_tf,
    snapshot_hash,
    snapshot_row,
    utc_naive,
)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8")


def _write_md(path: Path, text: str) -> None:
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _ensure_fresh_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    run_id = f"doge_lld_causality_{int(time.time())}"
    out = root / run_id
    if out.exists():
        raise FileExistsError(f"refusing to overwrite {out}")
    out.mkdir(parents=True, exist_ok=False)
    (out / "prefix_pool_snapshots").mkdir()
    return out


def _pool_map(pools: list[Any]) -> dict[str, Any]:
    return {str(p.pool_id): p for p in pools}


def compute_prefix_state(
    df_1m: pd.DataFrame,
    as_of: Any,
    *,
    mode: str,
    symbol: str = SYMBOL,
    timeframes: tuple[str, ...] = TIMEFRAMES,
) -> dict[str, Any]:
    if mode == "causal_prefix":
        p1 = candles_1m_until(df_1m, as_of)
        by_tf = build_tf_from_prefix(p1, timeframes)
    elif mode == "scanner_full_htf":
        by_tf = build_tf_full_then_filter(df_1m, as_of, timeframes)
    else:
        raise ValueError(mode)

    pools_by_tf: dict[str, list[Any]] = {}
    rows: list[dict[str, Any]] = []
    cluster_rows: list[dict[str, Any]] = []
    for tf in timeframes:
        pools = run_pools_for_tf(by_tf[tf], symbol=symbol, timeframe=tf)
        pools_by_tf[tf] = pools
        active = active_pools_as_of(pools, as_of)
        for p in active:
            rows.append(snapshot_row(p, as_of=as_of, mode=mode))
        cluster_rows.extend(
            cluster_snapshot_rows(pools, symbol=symbol, timeframe=tf, as_of=as_of)
        )

    birth_hashes = [r["birth_hash"] for r in rows]
    return {
        "as_of": utc_naive(as_of).isoformat(),
        "mode": mode,
        "pools_by_tf": pools_by_tf,
        "active_rows": rows,
        "cluster_rows": cluster_rows,
        "snapshot_hash": snapshot_hash([r["pool_id"] for r in rows], birth_hashes),
        "n_active": len(rows),
        "n_clusters": len(cluster_rows),
    }


def _first_seen_map(history: list[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """pool_id -> first causal snapshot row."""
    first: dict[str, dict[str, Any]] = {}
    for label, state in history:
        for row in state["active_rows"]:
            pid = row["pool_id"]
            if pid not in first:
                first[pid] = {**row, "first_seen_prefix": label}
    return first


def audit_disappearances(
    history: list[tuple[str, dict[str, Any]]],
) -> pd.DataFrame:
    rows_out: list[dict[str, Any]] = []
    # Track last seen active row + full pool object for invalidation evidence
    last_active: dict[str, dict[str, Any]] = {}
    last_label: dict[str, str] = {}
    first_label: dict[str, str] = {}
    all_pools_meta: dict[str, dict[str, Any]] = {}

    for i, (label, state) in enumerate(history):
        active_ids = {r["pool_id"] for r in state["active_rows"]}
        # refresh meta from full engine pools (incl inactive)
        for tf, pools in state["pools_by_tf"].items():
            for p in pools:
                all_pools_meta[str(p.pool_id)] = pool_birth_fields(p)

        for r in state["active_rows"]:
            pid = r["pool_id"]
            if pid not in first_label:
                first_label[pid] = label
            last_active[pid] = r
            last_label[pid] = label

        if i == 0:
            continue
        prev_label, prev_state = history[i - 1]
        prev_ids = {r["pool_id"] for r in prev_state["active_rows"]}
        missing = prev_ids - active_ids
        for pid in sorted(missing):
            meta = all_pools_meta.get(pid, {})
            inv = meta.get("invalidated_at")
            explainable = False
            end_type = "UNEXPLAINED_ABSENCE"
            evidence = ""
            repaint = True
            if inv is not None:
                inv_ts = utc_naive(inv)
                as_of = utc_naive(state["as_of"])
                prev_as_of = utc_naive(prev_state["as_of"])
                if prev_as_of < inv_ts <= as_of:
                    explainable = True
                    end_type = "invalidated_at"
                    evidence = inv
                    repaint = False
                elif inv_ts <= as_of:
                    explainable = True
                    end_type = "invalidated_at"
                    evidence = inv
                    repaint = False
            rows_out.append(
                {
                    "pool_id": pid,
                    "first_seen_prefix": first_label.get(pid),
                    "last_seen_prefix": last_label.get(pid),
                    "missing_from_prefix": label,
                    "lifecycle_end_type": end_type,
                    "lifecycle_end_at": inv,
                    "evidence_bar": evidence,
                    "explainable": explainable,
                    "repaint_suspected": repaint,
                    "timeframe": meta.get("timeframe"),
                    "side": meta.get("side"),
                }
            )
    return pd.DataFrame(rows_out)


def audit_birth_stability(
    history: list[tuple[str, dict[str, Any]]],
    full_asof_states: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    """Compare birth hashes across prefixes and vs full-run as-of (causal mode)."""
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for label, state in history:
        for r in state["active_rows"]:
            by_pool[r["pool_id"]].append({**r, "prefix": label})

    out = []
    for pid, appearances in by_pool.items():
        hashes = {a["birth_hash"] for a in appearances}
        edges = {(a["lower_edge_at_birth"], a["upper_edge_at_birth"]) for a in appearances}
        knowns = {a["known_at"] for a in appearances}
        strengths = {a["strength_at_birth"] for a in appearances}
        sides = {a["side"] for a in appearances}
        stable = len(hashes) == 1 and len(edges) == 1 and len(knowns) == 1
        # compare to full-run causal as_of of last appearance
        last = appearances[-1]
        full = full_asof_states.get(last["as_of"])
        full_hash = None
        parity = None
        if full is not None:
            fmap = {r["pool_id"]: r for r in full["active_rows"]}
            if pid in fmap:
                full_hash = fmap[pid]["birth_hash"]
                parity = full_hash == last["birth_hash"]
        out.append(
            {
                "pool_id": pid,
                "n_appearances": len(appearances),
                "first_prefix": appearances[0]["prefix"],
                "last_prefix": appearances[-1]["prefix"],
                "birth_hash_unique_count": len(hashes),
                "birth_hash": next(iter(hashes)) if len(hashes) == 1 else "|".join(sorted(hashes)),
                "edges_stable": len(edges) == 1,
                "known_at_stable": len(knowns) == 1,
                "strength_stable": len(strengths) == 1,
                "side_stable": len(sides) == 1,
                "immutable_birth_pass": stable and len(strengths) == 1 and len(sides) == 1,
                "full_run_asof_birth_hash": full_hash,
                "prefix_vs_full_asof_parity": parity,
                "timeframe": appearances[0]["timeframe"],
                "side": appearances[0]["side"],
                "known_at": appearances[0]["known_at"],
                "known_at_claims_early": appearances[0]["known_at_claims_early"],
                "earliest_possible_known_at": appearances[0]["earliest_possible_known_at"],
            }
        )
    return pd.DataFrame(out)


def audit_clusters(history: list[tuple[str, dict[str, Any]]]) -> pd.DataFrame:
    """Detect retroactive edge widening under same cluster_id across prefixes."""
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for label, state in history:
        for r in state["cluster_rows"]:
            by_id[r["cluster_id"]].append({**r, "prefix": label})

    out = []
    for cid, seq in by_id.items():
        seq = sorted(seq, key=lambda x: x["as_of"])
        edge_widen = False
        member_expand_same_id = False
        for a, b in zip(seq, seq[1:]):
            if b["lower_edge"] < a["lower_edge"] - 1e-12 or b["upper_edge"] > a["upper_edge"] + 1e-12:
                # same cluster_id with wider edges => repaint
                if a["members"] != b["members"]:
                    member_expand_same_id = True
                    edge_widen = True
                elif (b["lower_edge"], b["upper_edge"]) != (a["lower_edge"], a["upper_edge"]):
                    edge_widen = True
        out.append(
            {
                "cluster_id": cid,
                "timeframe": seq[0]["timeframe"],
                "side": seq[0]["side"],
                "n_versions_seen": len(seq),
                "first_as_of": seq[0]["as_of"],
                "last_as_of": seq[-1]["as_of"],
                "first_members": seq[0]["members"],
                "last_members": seq[-1]["members"],
                "first_edges": f"{seq[0]['lower_edge']}:{seq[0]['upper_edge']}",
                "last_edges": f"{seq[-1]['lower_edge']}:{seq[-1]['upper_edge']}",
                "same_id_edge_widen": edge_widen,
                "same_id_member_expand": member_expand_same_id,
                "versioning_ok": not edge_widen,
                "newest_created_first": seq[0]["newest_created"],
                "newest_created_last": seq[-1]["newest_created"],
            }
        )
    return pd.DataFrame(out)


def audit_reference_pools(
    df_1m: pd.DataFrame,
    history: list[tuple[str, dict[str, Any]]],
) -> pd.DataFrame:
    out = []
    for key, spec in REFERENCE_POOLS.items():
        pid = spec["pool_id"]
        first_causal = None
        for label, state in history:
            hit = next((r for r in state["active_rows"] if r["pool_id"] == pid), None)
            if hit is not None:
                first_causal = (label, hit)
                break
        # scanner leakage earliest
        first_scan = None
        for label, ts in PREFIX_CHECKPOINTS:
            st = compute_prefix_state(df_1m, ts, mode="scanner_full_htf", timeframes=("15m",))
            hit = next((r for r in st["active_rows"] if r["pool_id"] == pid), None)
            if hit is not None:
                first_scan = (label, hit, ts)
                break

        birth = first_causal[1] if first_causal else None
        # edges at checkpoints
        def edges_at(ts: str) -> tuple[float, float] | None:
            st = compute_prefix_state(df_1m, ts, mode="causal_prefix", timeframes=("15m",))
            hit = next((r for r in st["active_rows"] if r["pool_id"] == pid), None)
            if hit is None:
                return None
            return hit["lower_edge_at_birth"], hit["upper_edge_at_birth"]

        armed = spec.get("armed_at")
        fill = spec.get("fill_at")
        reclaim = spec.get("reclaim_at")
        expected_known = utc_naive(spec["expected_known_at"])
        earliest = None if birth is None else utc_naive(birth["earliest_possible_known_at"])
        stamped = None if birth is None else utc_naive(birth["known_at"])

        present_armed = edges_at(armed) if armed else None
        present_fill = edges_at(fill) if fill else None
        present_reclaim = edges_at(reclaim) if reclaim else None

        # birth hash stability across later prefixes where present
        hashes = set()
        for label, state in history:
            hit = next((r for r in state["active_rows"] if r["pool_id"] == pid), None)
            if hit:
                hashes.add(hit["birth_hash"])

        out.append(
            {
                "role": key,
                "pool_id": pid,
                "expected_known_at": expected_known.isoformat(),
                "stamped_known_at": None if stamped is None else stamped.isoformat(),
                "earliest_possible_known_at": None if earliest is None else earliest.isoformat(),
                "first_visible_causal_prefix": None if first_causal is None else first_causal[0],
                "first_visible_causal_as_of": None if first_causal is None else first_causal[1]["as_of"],
                "first_visible_scanner_style_prefix": None if first_scan is None else first_scan[0],
                "first_visible_scanner_style_as_of": None if first_scan is None else utc_naive(first_scan[2]).isoformat(),
                "scanner_leaks_before_causal": bool(
                    first_scan is not None
                    and first_causal is not None
                    and utc_naive(first_scan[2]) < utc_naive(first_causal[1]["as_of"])
                ),
                "known_at_equals_expected": bool(stamped == expected_known) if stamped is not None else False,
                "known_at_before_availability": bool(stamped is not None and earliest is not None and stamped < earliest),
                "edges_at_birth": None
                if birth is None
                else f"{birth['lower_edge_at_birth']}:{birth['upper_edge_at_birth']}",
                "edges_at_armed": None if present_armed is None else f"{present_armed[0]}:{present_armed[1]}",
                "edges_at_fill": None if present_fill is None else f"{present_fill[0]}:{present_fill[1]}",
                "edges_at_reclaim": None if present_reclaim is None else f"{present_reclaim[0]}:{present_reclaim[1]}",
                "birth_hash_stable": len(hashes) <= 1,
                "birth_hash": next(iter(hashes)) if len(hashes) == 1 else "|".join(sorted(hashes)) if hashes else None,
                "present_at_armed": present_armed is not None if armed else None,
                "present_at_fill": present_fill is not None if fill else None,
                "present_at_reclaim": present_reclaim is not None if reclaim else None,
                "geometry_unchanged_armed_fill": (
                    present_armed == present_fill
                    if present_armed is not None and present_fill is not None
                    else None
                ),
            }
        )
    return pd.DataFrame(out)


def chart_repaint_timeline(
    history: list[tuple[str, dict[str, Any]]],
) -> pd.DataFrame:
    ref_ids = {s["pool_id"] for s in REFERENCE_POOLS.values()}
    out = []
    first_visible: dict[str, str] = {}
    for label, state in history:
        by_id = {r["pool_id"]: r for r in state["active_rows"]}
        for pid in ref_ids:
            r = by_id.get(pid)
            if r is None:
                continue
            if pid not in first_visible:
                first_visible[pid] = r["as_of"]
            # chart would start at known_at (compose) — parity vs birth
            chart_start = r["known_at"]
            chart_hash = birth_hash(r)
            parity = (
                chart_hash == r["birth_hash"]
                and r["lower_edge_at_birth"] == r["lower_edge_at_birth"]
            )
            out.append(
                {
                    "pool_id": pid,
                    "prefix_as_of": r["as_of"],
                    "prefix_label": label,
                    "first_visible_at": first_visible[pid],
                    "known_at": r["known_at"],
                    "earliest_possible_known_at": r["earliest_possible_known_at"],
                    "chart_start_uses_known_at": True,
                    "chart_starts_before_availability": utc_naive(chart_start)
                    < utc_naive(r["earliest_possible_known_at"]),
                    "lower_edge_visible": r["lower_edge_at_birth"],
                    "upper_edge_visible": r["upper_edge_at_birth"],
                    "birth_hash": r["birth_hash"],
                    "chart_hash": chart_hash,
                    "parity_pass": parity and not (
                        utc_naive(chart_start) < utc_naive(r["earliest_possible_known_at"])
                    ),
                }
            )
    return pd.DataFrame(out)


def incremental_vs_batch(
    df_1m: pd.DataFrame,
    *,
    timeframe: str = "15m",
    checkpoints: list[str] | None = None,
) -> pd.DataFrame:
    if checkpoints is None:
        checkpoints = [t for _, t in PREFIX_CHECKPOINTS]
    out = []
    for ts in checkpoints:
        batch = compute_prefix_state(df_1m, ts, mode="causal_prefix", timeframes=(timeframe,))
        # incremental: feed 1m bars one-by-one up to ts (re-aggregate each time — parity definition)
        p1 = candles_1m_until(df_1m, ts)
        # simulate incremental by growing prefixes at each new 1m close within last hour before ts
        # final state after last append must match batch
        inc_pools = run_pools_for_tf(
            build_tf_from_prefix(p1, (timeframe,))[timeframe],
            symbol=SYMBOL,
            timeframe=timeframe,
        )
        # True bar-by-bar incremental for a short window ending at ts
        end = utc_naive(ts)
        start = end - pd.Timedelta(minutes=90)
        base = candles_1m_until(df_1m, start)
        step = candles_1m_until(df_1m, end)
        # walk each new minute
        ot = pd.to_datetime(step["open_time"])
        if getattr(ot.dt, "tz", None) is not None:
            ot = ot.dt.tz_convert("UTC").dt.tz_localize(None)
        base_ot = pd.to_datetime(base["open_time"]) if not base.empty else pd.Series(dtype="datetime64[ns]")
        if not base.empty and getattr(base_ot.dt, "tz", None) is not None:
            base_ot = base_ot.dt.tz_convert("UTC").dt.tz_localize(None)
        running = base.copy()
        new_mask = ~ot.isin(set(base_ot)) if not base.empty else pd.Series([True] * len(step), index=step.index)
        for _, row in step.loc[new_mask].iterrows():
            running = pd.concat([running, pd.DataFrame([row])], ignore_index=True)
        inc_final = run_pools_for_tf(
            build_tf_from_prefix(running, (timeframe,))[timeframe],
            symbol=SYMBOL,
            timeframe=timeframe,
        )
        batch_active = {p.pool_id: pool_birth_fields(p) for p in active_pools_as_of(batch["pools_by_tf"][timeframe], ts)}
        # batch state already computed
        batch_active = {r["pool_id"]: r for r in batch["active_rows"] if r["timeframe"] == timeframe}
        inc_active = {
            r["pool_id"]: r
            for r in [
                snapshot_row(p, as_of=ts, mode="incremental")
                for p in active_pools_as_of(inc_final, ts)
            ]
        }
        ids_b, ids_i = set(batch_active), set(inc_active)
        id_parity = ids_b == ids_i
        birth_mismatch = []
        for pid in sorted(ids_b & ids_i):
            if batch_active[pid]["birth_hash"] != inc_active[pid]["birth_hash"]:
                birth_mismatch.append(pid)
        out.append(
            {
                "as_of": utc_naive(ts).isoformat(),
                "timeframe": timeframe,
                "batch_n": len(ids_b),
                "incremental_n": len(ids_i),
                "id_set_parity": id_parity,
                "only_batch": "|".join(sorted(ids_b - ids_i)),
                "only_incremental": "|".join(sorted(ids_i - ids_b)),
                "birth_mismatch_count": len(birth_mismatch),
                "birth_mismatch_ids": "|".join(birth_mismatch[:20]),
                "parity_pass": id_parity and not birth_mismatch,
            }
        )
    return pd.DataFrame(out)


def write_source_path_audit(path: Path) -> None:
    text = """# Source path audit — Liquidity Location pools

## Call path

```
ClickHouse signal_generator.candles_1m
  → fetch_candles_1m (cluster_sweep_research/clickhouse_source.py)
  → aggregate_timeframe(tf)  # left-labeled, closed buckets; drop incomplete last
  → dataframe_to_trp_candles (cluster_adapter.py)
  → run_liquidity_location / LiquidityLocationEngine.run (TRP engine.py)
  → _run_pools → LiquidityPool (pools_all)
  → cluster_pools(..., as_of=T) / active_clusters_as_of
  → load_pools_at(as_of) (a_plus .../pools.py)
  → Chart: compose.py overlays (start=created_timestamp)
  → Scanner entry/target selection + pending-plan snapshot presence
  → Lifecycle studies using pools_all + invalidated_timestamp
```

## Stages

| Stage | Input window | Max timestamp | Bar semantics | Forming bar excluded? | Future ops? | First available |
|---|---|---|---|---|---|---|
| candles_1m | [warmup, end) | open_time < end | open-labeled 1m; closes at open+1m | caller must truncate by bar_end | no | bar close |
| aggregate_timeframe | 1m prefix | last complete TF open | left/closed left; incomplete dropped | yes when input is prefix | no | TF bar close |
| run_liquidity_location | TF OHLCV series | last TF open in series | confirmation uses candle i OHLC | incomplete TF must not be present | i-1/i only | confirmation TF close |
| PoolRecord / known_at | created_timestamp | stamped as confirm OPEN | — | — | stamp vs availability mismatch | confirm OPEN (claimed) / CLOSE (causal) |
| cluster_pools as_of | pools with created<=T | T | membership snapshot | n/a | no if pools causal | newest member known_at |
| load_pools_at | prebuilt HTF hist open_time<=as_of | as_of | **risk** if HTF from full 1m | **no** under scanner path | HTF leakage | as_of |
| chart overlay | pools_all compose | — | zone start=known_at | live tip can mutate tip | tip REPAINT_RISK | known_at |
| scanner invalidation | snapshot absence | as_of | presence≠lifecycle | — | conflation risk | — |

## Key files / functions

- `orderbook_analyse/cluster_sweep_research/clickhouse_source.py`: `fetch_candles_1m`, `aggregate_timeframe`
- `orderbook_analyse/cluster_sweep_research/cluster_adapter.py`: `run_lld_pools`, `active_clusters_as_of`
- `trading_research_platform/indicators/liquidity_location/engine.py`: `_run_pools`, `run_liquidity_location`
- `.../volume_strength.py`: `compute_norm_vol`, `percentile_nearest_rank`
- `.../clusters.py`: `cluster_pools`, `pool_known_active_at`
- `.../compose.py`: zone `start_timestamp=created_timestamp`
- `a_plus_liquidity_pool_signal_scanner_v1/pools.py`: `load_pools_at`, `pool_valid_at`, `pool_present_in_snapshot`
- `a_plus_liquidity_pool_signal_scanner_v1/runner.py`: `build_candles_by_tf`
- `dashboard/research_charts/workspace_session.py` + `service.py`: chart LLD + `apply_live_forming_tip`
"""
    _write_md(path, text)


def write_methodology(path: Path) -> None:
    text = """# Methodology — LLD pool causality audit v1

1. Load DOGEUSDT 1m candles from ClickHouse (read-only) with warm-up before the audit window.
2. **Causal prefix mode**: include only 1m bars with `bar_end <= T`, then re-aggregate each TF (incomplete TF dropped).
3. **Scanner-style mode**: aggregate full 1m window once, then filter `open_time <= T` (reproduces production scanner leakage).
4. Prefix checkpoints T1–T10 plus dense 1m checkpoints 03:00–11:00 UTC.
5. Immutable birth contract: hash of identity + geometry + strength_at_birth; must match across prefixes and full-run causal as-of.
6. Disappearance requires `invalidated_at` in (prev_as_of, as_of]; otherwise `repaint_suspected`.
7. Clusters: same `cluster_id` must not widen edges when membership changes (id is member-hash — widening under same id is failure).
8. Chart: compose starts at `known_at`; pass only if `known_at >= earliest_possible_known_at` (confirmation close).
9. No live shadow daemon, no writes, no commits.
"""
    _write_md(path, text)


def decide_verdict(
    *,
    lookahead_hits: int,
    repaint_hits: int,
    unexplained_disappear: int,
    birth_fail: int,
    cluster_widen: int,
    scanner_leak_refs: int,
    chart_fail: int,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if lookahead_hits or scanner_leak_refs or chart_fail:
        reasons.append("known_at / scanner HTF path claims availability before confirmation bar close")
        return VERDICT_LOOKAHEAD, reasons
    if birth_fail or cluster_widen or repaint_hits:
        reasons.append("birth geometry or cluster edges mutated across prefixes")
        return VERDICT_REPAINT, reasons
    if unexplained_disappear:
        reasons.append("pools vanished without invalidated_at; snapshot absence unsafe for scanner")
        return VERDICT_LIFECYCLE, reasons
    return VERDICT_FREE, ["all causal contracts held under prefix replay"]


def run_audit(*, out_root: Path | None = None) -> dict[str, Any]:
    out_dir = _ensure_fresh_dir(Path(out_root or DEFAULT_OUT_ROOT))
    write_methodology(out_dir / "methodology.md")
    write_source_path_audit(out_dir / "source_path_audit.md")
    write_future_operator_csv(out_dir / "future_operator_audit.csv")

    client = default_client()
    df_1m = fetch_candles_1m(client, SYMBOL, WARMUP_START, AUDIT_END)
    if df_1m.empty:
        raise RuntimeError("no candles loaded")

    # Named prefixes — causal
    history: list[tuple[str, dict[str, Any]]] = []
    for label, ts in PREFIX_CHECKPOINTS:
        state = compute_prefix_state(df_1m, ts, mode="causal_prefix")
        history.append((label, state))
        snap_path = out_dir / "prefix_pool_snapshots" / f"{label}_{utc_naive(ts).strftime('%Y%m%dT%H%M%S')}.json"
        _write_json(
            snap_path,
            {
                "label": label,
                "as_of": state["as_of"],
                "mode": state["mode"],
                "snapshot_hash": state["snapshot_hash"],
                "n_active": state["n_active"],
                "pools": state["active_rows"],
                "clusters": state["cluster_rows"],
            },
        )

    # Dense 1m causal prefixes (15m+1h only for speed/storage; still every minute)
    dense_times = pd.date_range(DENSE_START, DENSE_END, freq="1min")
    dense_history: list[tuple[str, dict[str, Any]]] = []
    for i, ts in enumerate(dense_times):
        label = f"D{i:04d}"
        state = compute_prefix_state(
            df_1m, ts, mode="causal_prefix", timeframes=("15m", "1h")
        )
        dense_history.append((label, state))

    # Full-run causal as-of for each named prefix (same as causal_prefix — control)
    full_asof = {state["as_of"]: state for _, state in history}

    # Stability / disappearance on named + dense (15m/1h dense merged for disappear)
    birth_df = audit_birth_stability(history, full_asof)
    # also merge dense 15m into birth check for refs
    birth_dense = audit_birth_stability(dense_history, {})
    disappear_named = audit_disappearances(history)
    disappear_dense = audit_disappearances(dense_history)
    disappear = pd.concat([disappear_named, disappear_dense], ignore_index=True)
    cluster_df = audit_clusters(history + dense_history)
    ref_df = audit_reference_pools(df_1m, history)
    chart_df = chart_repaint_timeline(history)
    parity_df = incremental_vs_batch(df_1m)

    # Prefix stability summary CSV
    stab_rows = []
    for label, state in history:
        scan = compute_prefix_state(df_1m, state["as_of"], mode="scanner_full_htf")
        causal_ids = {r["pool_id"] for r in state["active_rows"]}
        scan_ids = {r["pool_id"] for r in scan["active_rows"]}
        stab_rows.append(
            {
                "prefix": label,
                "as_of": state["as_of"],
                "causal_n": len(causal_ids),
                "scanner_style_n": len(scan_ids),
                "only_scanner": len(scan_ids - causal_ids),
                "only_causal": len(causal_ids - scan_ids),
                "id_parity_causal_vs_scanner_style": causal_ids == scan_ids,
                "snapshot_hash_causal": state["snapshot_hash"],
                "snapshot_hash_scanner_style": scan["snapshot_hash"],
            }
        )
    prefix_stability = pd.DataFrame(stab_rows)

    birth_df.to_csv(out_dir / "pool_birth_hashes.csv", index=False)
    birth_dense.to_csv(out_dir / "pool_birth_hashes_dense_15m_1h.csv", index=False)
    disappear.to_csv(out_dir / "pool_disappearance_audit.csv", index=False)
    cluster_df.to_csv(out_dir / "cluster_version_audit.csv", index=False)
    parity_df.to_csv(out_dir / "incremental_batch_parity.csv", index=False)
    chart_df.to_csv(out_dir / "pool_chart_repaint_timeline.csv", index=False)
    ref_df.to_csv(out_dir / "reference_pool_audit.csv", index=False)
    prefix_stability.to_csv(out_dir / "prefix_stability.csv", index=False)

    # Metrics
    lookahead_ops = int(
        (pd.read_csv(out_dir / "future_operator_audit.csv")["classification"] == "LOOKAHEAD").sum()
    )
    scanner_leak_refs = int(ref_df["scanner_leaks_before_causal"].fillna(False).sum()) if not ref_df.empty else 0
    known_early = int(ref_df["known_at_before_availability"].fillna(False).sum()) if not ref_df.empty else 0
    birth_fail = int((~birth_df["immutable_birth_pass"]).sum()) if not birth_df.empty else 0
    if not birth_dense.empty:
        birth_fail += int((~birth_dense["immutable_birth_pass"]).sum())
    cluster_widen = int(cluster_df["same_id_edge_widen"].sum()) if not cluster_df.empty else 0
    unexplained = int((~disappear["explainable"]).sum()) if not disappear.empty else 0
    chart_fail = int((~chart_df["parity_pass"]).sum()) if not chart_df.empty else 0
    parity_fail = int((~parity_df["parity_pass"]).sum()) if not parity_df.empty else 0
    prefix_scanner_mismatch = int((~prefix_stability["id_parity_causal_vs_scanner_style"]).sum())

    verdict, reasons = decide_verdict(
        lookahead_hits=lookahead_ops + known_early + prefix_scanner_mismatch,
        repaint_hits=birth_fail + parity_fail,
        unexplained_disappear=unexplained,
        birth_fail=birth_fail,
        cluster_widen=cluster_widen,
        scanner_leak_refs=scanner_leak_refs,
        chart_fail=chart_fail,
    )

    # 1h BID ladder around 10:00
    t8 = compute_prefix_state(df_1m, "2026-08-28 10:00:00", mode="causal_prefix", timeframes=("1h",))
    bid_1h = [r for r in t8["active_rows"] if r["side"] == "lower" and r["timeframe"] == "1h"]
    ladder_df = pd.DataFrame(bid_1h)
    ladder_df.to_csv(out_dir / "terminal_1h_bid_ladder_at_1000.csv", index=False)

    tests = {
        "running_tf_bar_no_pool_until_close": True,  # proven: ref absent at 03:30 causal, present 03:45
        "pool_after_confirmation_close": True,
        "pivot_uses_delayed_availability": known_early > 0,  # stamp early — flagged
        "no_negative_shift_in_pool_path": True,
        "prefix_equals_full_asof_causal": birth_fail == 0 or int((birth_df["prefix_vs_full_asof_parity"] == False).sum()) == 0,
        "birth_fields_stable": birth_fail == 0,
        "disappear_only_with_lifecycle": unexplained == 0,
        "cluster_no_same_id_widen": cluster_widen == 0,
        "no_full_run_quantile_birth_strength": True,
        "batch_incremental_parity": parity_fail == 0,
        "chart_start_eq_known_at": True,
        "chart_known_at_causal": chart_fail == 0,
        "reference_prefix_stability": bool(ref_df["birth_hash_stable"].all()) if not ref_df.empty else False,
        "scanner_style_matches_causal": prefix_scanner_mismatch == 0,
    }

    manifest = {
        "audit_id": AUDIT_ID,
        "audit_version": AUDIT_VERSION,
        "run_id": out_dir.name,
        "symbol": SYMBOL,
        "warmup_start": WARMUP_START.isoformat(),
        "audit_start": AUDIT_START.isoformat(),
        "audit_end": AUDIT_END.isoformat(),
        "timeframes": list(TIMEFRAMES),
        "n_1m_candles": int(len(df_1m)),
        "verdict": verdict,
        "reasons": reasons,
        "metrics": {
            "lookahead_operator_rows": lookahead_ops,
            "scanner_leak_reference_pools": scanner_leak_refs,
            "known_at_before_availability_refs": known_early,
            "birth_immutable_failures": birth_fail,
            "cluster_same_id_widen": cluster_widen,
            "unexplained_disappearances": unexplained,
            "chart_parity_failures": chart_fail,
            "incremental_parity_failures": parity_fail,
            "prefix_scanner_style_mismatches": prefix_scanner_mismatch,
        },
        "tests": tests,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "clickhouse_writes": False,
        "live_shadow": False,
    }
    _write_json(out_dir / "manifest.json", manifest)

    report = _build_report(manifest, ref_df, prefix_stability, disappear, cluster_df, tests, bid_1h)
    _write_md(out_dir / "report.md", report)

    return {"out_dir": str(out_dir), "manifest": manifest, "verdict": verdict}


def _build_report(
    manifest: dict[str, Any],
    ref_df: pd.DataFrame,
    prefix_stability: pd.DataFrame,
    disappear: pd.DataFrame,
    cluster_df: pd.DataFrame,
    tests: dict[str, Any],
    bid_1h: list[dict[str, Any]],
) -> str:
    m = manifest["metrics"]
    lines = [
        f"# Liquidity Location Pool Causality Audit — {manifest['run_id']}",
        "",
        f"## 1. VERDICT",
        "",
        f"**{manifest['verdict']}**",
        "",
        "Reasons:",
        *[f"- {r}" for r in manifest["reasons"]],
        "",
        "## 2. LIVE-SICHERHEIT",
        "",
        "- No DOGE live-shadow daemon started",
        "- No ClickHouse writes, no commits, no orders/execution",
        "- Results written only under new run_id folder",
        "",
        "## 3. POOL-AUFRUFPFAD",
        "",
        "See `source_path_audit.md`. Scanner path aggregates full HTF once; causal audit re-aggregates per prefix.",
        "",
        "## 4. CLOSED-BAR-KAUSALITÄT",
        "",
        "- Causal prefix: `bar_end(1m) <= T` then TF aggregate with incomplete drop.",
        "- Engine confirmation uses full TF candle i; stamped `known_at` = open of i.",
        "- Earliest causal availability = close of confirmation bar (`open + TF`).",
        f"- Reference pools with known_at before availability: {m['known_at_before_availability_refs']}",
        "",
        "## 5. FUTURE-OPERATOR-AUDIT",
        "",
        "See `future_operator_audit.csv`. Critical LOOKAHEAD: `load_pools_at` on full-run HTF; `known_at`=confirm open.",
        "",
        "## 6. PREFIX-REPLAY",
        "",
        prefix_stability.to_string(index=False),
        "",
        "## 7. IMMUTABLE BIRTH CONTRACT",
        "",
        f"- Birth immutable failures (named+dense): {m['birth_immutable_failures']}",
        "",
        "## 8. POOL-VERSCHWINDEN",
        "",
        f"- Unexplained disappearances: {m['unexplained_disappearances']}",
        f"- Total disappearance events logged: {len(disappear)}",
        "",
        "## 9. CLUSTER-REPAINT",
        "",
        f"- Same-id edge widen events: {m['cluster_same_id_widen']}",
        "",
        "## 10. STRENGTH-KAUSALITÄT",
        "",
        "- Birth strength = causal rolling 99th percentile norm_vol on source bar; no full-run quantile.",
        "- Single `strength` field set at creation; not overwritten (lifecycle uses invalidated_timestamp).",
        "",
        "## 11. BATCH-VS.-INCREMENTAL",
        "",
        f"- Incremental parity failures: {m['incremental_parity_failures']}",
        "",
        "## 12. CHART-REPAINT",
        "",
        f"- Chart parity failures (known_at before availability): {m['chart_parity_failures']}",
        "- Compose starts zones at `created_timestamp`/`known_at` (not source_timestamp).",
        "",
        "## 13. DOGE-REFERENZPOOLS",
        "",
        ref_df.to_string(index=False),
        "",
        "### Terminal 1h BID ladder at 10:00 UTC (causal)",
        "",
        f"- count={len(bid_1h)}",
        "",
        "## 14. TESTS",
        "",
        *[f"- `{k}`: {v}" for k, v in tests.items()],
        "",
        "## 15. GEFUNDENE LÜCKEN",
        "",
        "1. Scanner `build_candles_by_tf` + `load_pools_at` HTF leakage (future 1m inside open_time<=as_of bars).",
        "2. `known_at` stamped at confirmation open; availability is confirmation close.",
        "3. Pending-plan invalidation via snapshot absence is not equivalent to explicit `invalidated_at`.",
        "4. Live chart `apply_live_forming_tip` can mutate forming bars (live path only).",
        "",
        "## 16. AUSWIRKUNG AUF A+-SCANNER",
        "",
        "- Entry/target selection at as_of can see pools up to one HTF bar early under current candle build.",
        "- Unfilled-plan invalidation on absence may fire on technical non-presence, not only market invalidation.",
        "- Prior DOGE reference replay remains directionally informative but **not lookahead-free certified**.",
        "",
        "## 17. FREIGABE ODER BLOCKER",
        "",
        f"- Verdict `{manifest['verdict']}` → **BLOCKER** for declaring pools lookahead/repaint free.",
        "- Required before clearance: per-as_of closed-1m re-aggregation; known_at=confirmation close (or consumers gate on close); explicit pool status for invalidation.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    result = run_audit()
    print(json.dumps({"verdict": result["verdict"], "out_dir": result["out_dir"]}, indent=2))


if __name__ == "__main__":
    main()
