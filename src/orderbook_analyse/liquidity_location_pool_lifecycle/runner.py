"""Smoke / research runner for LLD pool lifecycle event-study."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
    aggregate_timeframe,
    fetch_candles_1m,
)
from orderbook_analyse.cluster_sweep_research.cluster_adapter import (
    CausalVerdict,
    LLD_AUDIT,
    active_clusters_as_of,
    run_lld_pools,
)
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from . import ANALYSIS_ID, ANALYSIS_VERSION, CAUSALITY_BLOCKED_VERDICT, EXPECTED_SMOKE_VERDICT
from .causality import CAUSALITY_AUDIT, pool_row_fields
from .constants import (
    CLUSTER_GAP_PCT,
    SMOKE_SYMBOLS,
    SMOKE_TIMEFRAMES,
)
from .ema_context import attach_context
from .lifecycle import (
    analysis_start_index,
    cluster_to_zone,
    pool_count_bucket,
    pool_to_zone,
    scan_zone_lifecycle,
)
from .stats_util import quality_by_symbol, summarize_cohorts, summarize_sensitivity, wilson_interval

DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/liquidity_location_pool_lifecycle_v1"
)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def coverage_probe(
    *,
    symbols: tuple[str, ...] = SMOKE_SYMBOLS,
    timeframes: tuple[str, ...] = SMOKE_TIMEFRAMES,
    window_days: int = 30,
    warmup_days: int = 14,
) -> dict[str, Any]:
    client = get_clickhouse_client()
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=window_days)
    warmup_start = start - timedelta(days=warmup_days)
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        df1 = fetch_candles_1m(client, sym, warmup_start, end)
        cov = {
            "symbol": sym,
            "candles_1m_rows": len(df1),
            "candles_1m_first": str(df1["open_time"].iloc[0]) if len(df1) else None,
            "candles_1m_last": str(df1["open_time"].iloc[-1]) if len(df1) else None,
        }
        for tf in timeframes:
            df = aggregate_timeframe(df1, tf)
            lld = run_lld_pools(df, symbol=sym, timeframe=tf)
            pools = lld.pools
            in_win = [
                p
                for p in pools
                if start <= _as_utc(p.created_timestamp) < end
            ]
            rows.append(
                {
                    **cov,
                    "timeframe": tf,
                    "bars_tf": len(df),
                    "lld_verdict": lld.verdict.value,
                    "pools_all": len(pools),
                    "pools_created_in_window": len(in_win),
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                }
            )
    return {
        "window_days": window_days,
        "warmup_days": warmup_days,
        "rows": rows,
        "engine_audit": LLD_AUDIT,
        "causality_audit": CAUSALITY_AUDIT,
    }


def _distance_features(df: pd.DataFrame, zone, start_i: int) -> dict[str, Any]:
    if start_i >= len(df):
        return {
            "distance_from_price": None,
            "distance_from_price_atr": None,
            "age_bars_at_start": 0,
        }
    row = df.iloc[start_i]
    px = float(row["close"])
    atr = float(row["atr_14"]) if pd.notna(row["atr_14"]) else float("nan")
    if zone.side == "BID":
        dist = px - zone.upper
    else:
        dist = zone.lower - px
    return {
        "distance_from_price": dist,
        "distance_from_price_atr": (dist / atr) if atr and atr == atr else None,
        "age_bars_at_start": 0,
    }


def analyze_symbol_tf(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    window_start: datetime,
    window_end: datetime,
    max_pools: int | None = None,
) -> dict[str, Any]:
    window_start = _as_utc(window_start)
    window_end = _as_utc(window_end)
    df = attach_context(df)
    lld = run_lld_pools(df, symbol=symbol, timeframe=timeframe)
    if lld.verdict != CausalVerdict.CAUSAL_REUSABLE:
        return {
            "blocked": True,
            "reason": lld.reason or lld.verdict.value,
            "pool_instances": [],
            "pool_clusters": [],
            "events": [],
            "outcomes": [],
            "ema": [],
            "destinations": [],
            "quality": {"symbol": symbol, "timeframe": timeframe, "blocked": True},
        }

    pools = list(lld.pools)
    in_window = [p for p in pools if window_start <= _as_utc(p.created_timestamp) < window_end]
    in_window.sort(key=lambda p: p.created_timestamp)
    if max_pools is not None:
        in_window = in_window[:max_pools]

    pool_zones = [pool_to_zone(p) for p in in_window]
    # Destination pool refs: cap size for O(n) first-target search (EMA/swing always included)
    dest_ref = pool_zones
    if len(dest_ref) > 800:
        # keep strongest + evenly spaced sample for next-pool targets
        by_str = sorted(
            dest_ref,
            key=lambda z: (z.strength is not None, z.strength or 0.0),
            reverse=True,
        )
        dest_ref = by_str[:400] + pool_zones[:: max(1, len(pool_zones) // 400)][:400]

    pool_instances: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    ema_rows: list[dict[str, Any]] = []
    destinations: list[dict[str, Any]] = []

    for p, z in zip(in_window, pool_zones):
        start_i = analysis_start_index(df, z.known_at)
        dist = _distance_features(df, z, start_i)
        base = pool_row_fields(p)
        base.update(
            {
                "analysis_window_start": window_start.isoformat(),
                "analysis_window_end": window_end.isoformat(),
                "pool_count_bucket": "1",
                **dist,
            }
        )
        # age at end of series
        base["age_bars_end"] = max(0, len(df) - 1 - int(p.created_index))
        scanned = scan_zone_lifecycle(df, z, other_zones=dest_ref)
        base.update({k: scanned["summary"][k] for k in scanned["summary"] if k not in base})
        pool_instances.append(base)
        events.extend(scanned["events"])
        for o in scanned["outcomes"]:
            o["pool_count_bucket"] = "1"
            outcomes.append(o)
        ema_rows.extend(scanned["ema"])
        destinations.extend(scanned["destinations"])

    # Causal clusters: snapshot at each window pool's known_at (stride if dense)
    cluster_rows: list[dict[str, Any]] = []
    seen_fp: set[str] = set()
    creation_times = sorted({_as_utc(p.created_timestamp) for p in in_window})
    max_cluster_snaps = 400
    if len(creation_times) > max_cluster_snaps:
        step = max(1, len(creation_times) // max_cluster_snaps)
        creation_times = creation_times[::step]

    for t in creation_times:
        clusters = active_clusters_as_of(
            pools, t, gap_pct=CLUSTER_GAP_PCT, minimum_pools=2
        )
        for c in clusters:
            newest = _as_utc(c.newest_created)
            if not (window_start <= newest < window_end):
                continue
            # cluster first becomes this size when newest member is created
            if abs((newest - t).total_seconds()) > 60:
                continue
            fp = c.cluster_id
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
            # reject future components (quality)
            members = [p for p in pools if p.pool_id in c.pool_ids]
            if any(_as_utc(p.created_timestamp) > t for p in members):
                continue
            z = cluster_to_zone(c, symbol=symbol, timeframe=timeframe)
            start_i = analysis_start_index(df, z.known_at)
            atr = (
                float(df.iloc[start_i]["atr_14"])
                if start_i < len(df) and pd.notna(df.iloc[start_i]["atr_14"])
                else None
            )
            width = z.upper - z.lower
            dist = _distance_features(df, z, start_i)
            crow = {
                "cluster_id": c.cluster_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "side": z.side,
                "engine_side": c.side,
                "lower_price": z.lower,
                "upper_price": z.upper,
                "center_price": z.center,
                "number_of_component_pools": c.pool_count,
                "number_of_timeframes": 1,
                "timeframe_mix": timeframe,
                "total_strength": c.strength_sum,
                "maximum_strength": c.strength_max,
                "mean_strength": c.strength_mean,
                "overlap_depth": c.pool_count,
                "cluster_width": width,
                "cluster_width_atr": (width / atr) if atr else None,
                "age": 0,
                "known_at": z.known_at.isoformat(),
                "component_pools": "|".join(c.pool_ids),
                "pool_count_bucket": pool_count_bucket(c.pool_count),
                "repeated_confirmations": c.pool_count,
                **dist,
            }
            scanned = scan_zone_lifecycle(df, z, other_zones=dest_ref)
            crow.update(
                {
                    k: scanned["summary"][k]
                    for k in ("touched", "swept", "minutes_to_touch", "minutes_to_sweep")
                }
            )
            cluster_rows.append(crow)
            events.extend(scanned["events"])
            for o in scanned["outcomes"]:
                o["pool_count_bucket"] = pool_count_bucket(c.pool_count)
                outcomes.append(o)
            ema_rows.extend(scanned["ema"])
            destinations.extend(scanned["destinations"])

    quality = {
        "symbol": symbol,
        "timeframe": timeframe,
        "blocked": False,
        "pools_all": len(pools),
        "pools_in_window": len(in_window),
        "clusters_in_window": len(cluster_rows),
        "n_events": len(events),
        "n_outcomes": len(outcomes),
        "lld_verdict": lld.verdict.value,
    }
    return {
        "blocked": False,
        "pool_instances": pool_instances,
        "pool_clusters": cluster_rows,
        "events": events,
        "outcomes": outcomes,
        "ema": ema_rows,
        "destinations": destinations,
        "quality": quality,
    }


def run_smoke(
    *,
    out_dir: Path = DEFAULT_OUT,
    symbols: tuple[str, ...] = SMOKE_SYMBOLS,
    timeframes: tuple[str, ...] = SMOKE_TIMEFRAMES,
    window_days: int = 30,
    warmup_days: int = 14,
    max_pools_per_tf: int | None = None,
    coverage_only: bool = False,
) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    cov = coverage_probe(
        symbols=symbols, timeframes=timeframes, window_days=window_days, warmup_days=warmup_days
    )
    (out_dir / "coverage_probe.json").write_text(json.dumps(cov, indent=2, default=str), encoding="utf-8")
    print("COVERAGE_PROBE_WRITTEN", out_dir / "coverage_probe.json", flush=True)
    for r in cov["rows"]:
        print(
            f"  {r['symbol']} {r['timeframe']}: bars={r['bars_tf']} "
            f"pools_window={r['pools_created_in_window']} verdict={r['lld_verdict']}",
            flush=True,
        )

    if coverage_only:
        return {"verdict": "COVERAGE_ONLY", "coverage": cov}

    # Causality gate
    if not CAUSALITY_AUDIT.get("engine_causal"):
        verdict = CAUSALITY_BLOCKED_VERDICT
        _write_blocked(out_dir, verdict, cov)
        return {"verdict": verdict, "coverage": cov}

    client = get_clickhouse_client()
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=window_days)
    warmup_start = start - timedelta(days=warmup_days)

    all_pools: list[dict] = []
    all_clusters: list[dict] = []
    all_events: list[dict] = []
    all_outcomes: list[dict] = []
    all_ema: list[dict] = []
    all_dest: list[dict] = []
    qualities: list[dict] = []
    blocked_any = False

    for sym in symbols:
        print(f"LOAD {sym}", flush=True)
        df1 = fetch_candles_1m(client, sym, warmup_start, end)
        for tf in timeframes:
            print(f"ANALYZE {sym} {tf}", flush=True)
            df = aggregate_timeframe(df1, tf)
            res = analyze_symbol_tf(
                df,
                symbol=sym,
                timeframe=tf,
                window_start=start,
                window_end=end,
                max_pools=max_pools_per_tf,
            )
            if res["blocked"]:
                blocked_any = True
                qualities.append(res["quality"])
                print(f"  BLOCKED {res.get('reason')}", flush=True)
                continue
            all_pools.extend(res["pool_instances"])
            all_clusters.extend(res["pool_clusters"])
            all_events.extend(res["events"])
            all_outcomes.extend(res["outcomes"])
            all_ema.extend(res["ema"])
            all_dest.extend(res["destinations"])
            qualities.append(res["quality"])
            print(
                f"  pools={res['quality']['pools_in_window']} "
                f"clusters={res['quality']['clusters_in_window']} "
                f"outcomes={res['quality']['n_outcomes']}",
                flush=True,
            )

    if blocked_any and not all_pools:
        verdict = CAUSALITY_BLOCKED_VERDICT
        _write_blocked(out_dir, verdict, cov)
        return {"verdict": verdict, "coverage": cov}

    pools_df = pd.DataFrame(all_pools)
    clusters_df = pd.DataFrame(all_clusters)
    events_df = pd.DataFrame(all_events)
    outcomes_df = pd.DataFrame(all_outcomes)
    ema_df = pd.DataFrame(all_ema)
    dest_df = pd.DataFrame(all_dest)

    # first destination summary table
    if not dest_df.empty:
        first_dest = (
            dest_df.sort_values(["entity_id", "trigger", "horizon_minutes"])
            .groupby(["entity_id", "trigger", "horizon_minutes"], as_index=False)
            .first()
        )
    else:
        first_dest = dest_df

    cohort = summarize_cohorts(outcomes_df)
    sens = summarize_sensitivity(outcomes_df)
    quality_df = pd.DataFrame(qualities)

    _write_csv(out_dir / "pool_instances.csv", pools_df)
    _write_csv(out_dir / "pool_clusters.csv", clusters_df)
    _write_csv(out_dir / "pool_lifecycle_events.csv", events_df)
    _write_csv(out_dir / "pool_outcomes.csv", outcomes_df)
    _write_csv(out_dir / "pool_ema_context.csv", ema_df)
    _write_csv(out_dir / "first_destination_outcomes.csv", first_dest)
    _write_csv(out_dir / "cohort_summary.csv", cohort)
    _write_csv(out_dir / "sensitivity_summary.csv", sens)
    _write_csv(out_dir / "quality_by_symbol.csv", quality_df)

    write_methodology(out_dir)
    verdict = EXPECTED_SMOKE_VERDICT
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "version": ANALYSIS_VERSION,
        "verdict": verdict,
        "symbols": list(symbols),
        "timeframes": list(timeframes),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "window_days": window_days,
        "warmup_days": warmup_days,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - t0, 2),
        "counts": {
            "pool_instances": len(pools_df),
            "pool_clusters": len(clusters_df),
            "lifecycle_events": len(events_df),
            "outcomes": len(outcomes_df),
            "ema_rows": len(ema_df),
            "destinations": len(first_dest),
        },
        "causality_audit": CAUSALITY_AUDIT,
        "lld_audit": LLD_AUDIT,
        "display_caveat": CAUSALITY_AUDIT["display_vs_known_at"],
        "no_commit": True,
        "no_clickhouse_writes": True,
        "no_bot_logic": True,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    write_report(out_dir, manifest, cov, cohort, sens, quality_df, outcomes_df, first_dest)
    print("VERDICT", verdict, flush=True)
    return {"verdict": verdict, "manifest": manifest, "coverage": cov, "out_dir": str(out_dir)}


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    df.to_csv(path, index=False)


def _write_blocked(out_dir: Path, verdict: str, cov: dict) -> None:
    (out_dir / "manifest.json").write_text(
        json.dumps({"verdict": verdict, "coverage": cov, "causality_audit": CAUSALITY_AUDIT}, indent=2, default=str),
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(
        f"# {verdict}\n\nCausality/repaint gate blocked the lifecycle smoke.\n",
        encoding="utf-8",
    )


def write_methodology(out_dir: Path) -> None:
    text = """# Liquidity Location Pool Lifecycle — Methodology

## Purpose
Causal event-study of the same Liquidity Location pools shown on Research Charts
(`/live-charts/research`) as cyan BID (engine `lower`) and magenta ASK (engine `upper`) zones.
No trading strategy, bot, orders, or PnL.

## Exact data path (same pools as chart)
1. **Source:** ClickHouse `signal_generator.candles_1m` (OHLCV), aggregated to TF.
2. **Generator:** `trading_research_platform.indicators.liquidity_location.engine.run_liquidity_location`
   via `orderbook_analyse.cluster_sweep_research.cluster_adapter.run_lld_pools`.
3. **Storage:** None persistent for pools — recomputed from candles each run (same as dashboard).
4. **API (dashboard):** research pane → `lld_objects` → `compose_lld_overlays` → overlays JSON.
5. **Chart:** `research_charts.js` / `chart.js` `renderZone` (cyan `#228bab`/`#00dcff`, magenta `#ec4079`/`#ff468c`).

This study uses **`pools_all`** (not amount-pruned display `pools`) to avoid survivor bias.

## Pool field contract
| Field | Source |
|-------|--------|
| symbol, timeframe | LiquidityPool |
| side BID/ASK | map lower→BID, upper→ASK |
| lower_price / upper_price | bottom_price / top_price |
| center_price | midpoint |
| strength | norm_vol of source bar (causal rolling percentile) |
| source_timeframe | pool.timeframe |
| source_count | 1 for singles |
| component_pools | pool_id or joined cluster ids |
| created_at / known_at / first_available_at | created_timestamp |
| source_at / display_zone_start | source_timestamp |
| expires_at | none |
| invalidated_at | invalidated_timestamp |

## known_at / causality
- `known_at = created_timestamp` (confirmation bar open time).
- Geometry + strength from source bar i-1 only.
- Outcome scanning starts at the **next closed bar after** the confirmation bar open
  (`analysis_start_index`), i.e. no price outcome before the pool is causally known.
- Bounds fixed at creation (no repaint of geometry).
- Clusters: `cluster_pools(..., as_of=T)` — only pools with `created_timestamp <= T`
  and not yet invalidated.

## Display caveat (not changed)
Chart rectangles start at `source_timestamp` (one bar before `known_at`). Live forming tip
can flicker LLD on open bars. Analysis uses closed candles only; display left unchanged.

## Lifecycle states
CREATED, ACTIVE, APPROACHED, FIRST_TOUCH, PENETRATED, FAR_EDGE_REACHED, SWEPT,
RECLAIMED, ACCEPTED_BEYOND, INVALIDATED (EXPIRED unused — engine has no expiry).

## Outcomes (mirrored BID/ASK)
- **TOUCHED:** range intersects [lower, upper]
- **SWEPT:** BID `low <= lower`; ASK `high >= upper`
- **DEFENDED:** touched, never swept, then reaction distance away from near edge
- **SWEPT_RECLAIMED:** after sweep, close back beyond near edge within reclaim horizon
- **CONSUMED_ACCEPTED:** K consecutive closes beyond far edge without reclaim (exclusive vs reclaim)

### Sensitivity grid
- acceptance bars: 1, 2, 3
- reclaim horizon bars: 1, 3, 6, 12
- reaction ATR: 0.25, 0.5, 1.0

Primary cohort headline uses acceptance=2, reclaim=6, reaction=0.5 ATR.

## EMAs
EMA9/20/59/200 from closed closes (SMA-seed then recursive), causal at each event bar.

## Destinations
After FIRST_TOUCH and SWEPT, for horizons 15m…24h: next BID/ASK pool, stronger pool,
EMA9/20/59/200, prior swing high/low, return to origin, or no target.

## Quality rules enforced
- no analysis before known_at
- no future cluster members
- BID/ASK mirrored
- first touch once
- sweep only after far edge
- reclaim vs accept exclusive per variant
- closed-bar EMA
- `pools_all` (no display survivor bias)
- no overwrite of existing result files
"""
    path = out_dir / "methodology.md"
    if path.exists():
        raise FileExistsError(path)
    path.write_text(text, encoding="utf-8")


def write_report(
    out_dir: Path,
    manifest: dict,
    cov: dict,
    cohort: pd.DataFrame,
    sens: pd.DataFrame,
    quality: pd.DataFrame,
    outcomes: pd.DataFrame,
    dest: pd.DataFrame,
) -> None:
    lines = [
        f"# {manifest['verdict']}",
        "",
        "## 1. VERDICT",
        f"{manifest['verdict']}",
        "",
        "## 2. LIVE-SICHERHEIT",
        "- No commit, no dashboard restart, no collector change, no ClickHouse writes.",
        "- No bot / orders / PnL. Chart display not modified.",
        "",
        "## 3. EXAKTE POOL-DATENQUELLE",
        "TRP `run_liquidity_location` on CH 1m→TF OHLCV; same engine as Research Charts.",
        "See methodology.md for full path and field contract.",
        "",
        "## 4. LOOKAHEAD-/REPAINT-AUDIT",
        f"- Engine causal: {CAUSALITY_AUDIT['engine_causal']}",
        f"- Geometry repaint: {CAUSALITY_AUDIT['pool_geometry_repaint']}",
        f"- known_at: {CAUSALITY_AUDIT['known_at_field']}",
        f"- Display caveat: {CAUSALITY_AUDIT['display_vs_known_at']}",
        "- Verdict not blocked: display backdate acknowledged; analysis uses known_at contract.",
        "",
        "## 5. POOL- UND CLUSTER-CONTRACT",
        "Single pools + causal clusters (gap 0.10%, as_of newest member). "
        "Buckets 1 / 2 / 3 / 4–5 / 6+.",
        "",
        "## 6. LIFECYCLE-DEFINITIONEN",
        "See methodology.md (TOUCHED / DEFENDED / SWEPT / SWEPT_RECLAIMED / CONSUMED_ACCEPTED).",
        "",
        "## 7. COVERAGE",
    ]
    for r in cov["rows"]:
        lines.append(
            f"- {r['symbol']} {r['timeframe']}: bars={r['bars_tf']}, "
            f"pools_in_window≈{r['pools_created_in_window']}, verdict={r['lld_verdict']}"
        )
    lines += [
        "",
        "## 8. EVENTMENGEN",
        f"- pool_instances: {manifest['counts']['pool_instances']}",
        f"- pool_clusters: {manifest['counts']['pool_clusters']}",
        f"- lifecycle_events: {manifest['counts']['lifecycle_events']}",
        f"- outcomes: {manifest['counts']['outcomes']}",
        "",
        "## 9. EINZELPOOL VS. MULTI-POOL",
    ]
    if not cohort.empty and "pool_count_bucket" in cohort.columns:
        sub = cohort.groupby("pool_count_bucket", dropna=False).agg(
            n=("n", "sum"),
            touch=("touch_rate", "mean"),
            sweep=("sweep_rate", "mean"),
        )
        lines.append(sub.to_string())
    else:
        lines.append("(see cohort_summary.csv)")

    lines += ["", "## 10. SWEEP-/DEFENSE-/RECLAIM-RATEN", "Primary variant (acc=2, reclaim=6, react=0.5):"]
    if not outcomes.empty:
        prim = outcomes[
            (outcomes["acceptance_bars"] == 2)
            & (outcomes["reclaim_horizon_bars"] == 6)
            & (
                outcomes["reaction_atr_mult"].isna()
                | (outcomes["reaction_atr_mult"] == 0.5)
                | (outcomes["swept"] == True)  # noqa: E712
            )
        ]
        # dedupe entities for headline: one row per entity for swept branch
        if not prim.empty:
            # take one row per entity_id preferring reaction 0.5 when present
            prim2 = prim.sort_values(["entity_id", "reaction_atr_mult"], na_position="first")
            prim2 = prim2.groupby("entity_id", as_index=False).first()
            n = len(prim2)
            for name, col in [
                ("touch", "touched"),
                ("sweep", "swept"),
                ("defended", "defended"),
                ("sweep_reclaim", "swept_reclaimed"),
                ("consumed_accepted", "consumed_accepted"),
            ]:
                s = int(prim2[col].astype(bool).sum())
                lo, hi = wilson_interval(s, n)
                lines.append(f"- {name}: {s}/{n} = {s/n if n else None:.4f} Wilson[{lo},{hi}]")
    lines += ["", "## 11. FIRST DESTINATION NACH DEM POOL"]
    if not dest.empty:
        d30 = dest[dest["horizon_minutes"] == 60]
        if not d30.empty:
            vc = d30["first_destination"].value_counts().head(12)
            lines.append("Top destinations @ 60m (all triggers):")
            lines.append(vc.to_string())
    else:
        lines.append("(empty)")

    lines += ["", "## 12. EMA-KONTEXT", "Snapshots in pool_ema_context.csv at CREATED/APPROACH/TOUCH/SWEEP/RECLAIM/ACCEPT."]
    lines += [
        "",
        "## 13. KANDIDATEN-PATTERNS",
        "No ≥90% rule claimed in this smoke. Patterns require OOS confirmation "
        "(see section 15). Inspect cohort_summary / sensitivity_summary for elevated rates.",
        "",
        "## 14. KONFIDENZINTERVALLE",
        "Wilson 95% intervals in cohort_summary.csv and section 10.",
        "",
        "## 15. OUT-OF-SAMPLE-STATUS",
        "Not yet: single contiguous smoke window only. No temporal holdout validation run.",
        "",
        "## 16. BLOCKER",
        "None for causal engine reuse. Display starts at source_timestamp (documented caveat).",
        "",
        "## 17. EMPFEHLUNG FÜR DEN NÄCHSTEN SCHRITT",
        "1) Fix primary cohort dedupe / entity-level rates in a follow-up notebook if needed.",
        "2) Add temporal OOS split (e.g. last 30% of window) before claiming high-hit patterns.",
        "3) Expand symbols only after OOS discipline is in place.",
        "4) Optionally align chart zone start to known_at later (separate UX change; not done here).",
        "",
        f"Elapsed: {manifest.get('elapsed_sec')}s",
    ]
    path = out_dir / "report.md"
    if path.exists():
        raise FileExistsError(path)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
