"""Continuous discovery runner — autonomous, read-only, no trade compiler."""

from __future__ import annotations

import json
import resource
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
    fetch_liquidations,
    fetch_oi_1m,
)
from orderbook_analyse.ema_zone_microstructure_confirmation import PLUGIN_ID, STRATEGY_YAML
from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_controls import (
    build_controls,
    matched_control_summary,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_defaults import (
    FORMAT_VERSION,
    OUT_SUBDIR,
    SAMPLE_MS,
    SYMBOLS_DEFAULT,
    continuous_research_defaults,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_engine import (
    process_symbol_stream,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_labels import (
    build_price_path,
    label_outcomes_for_candidates,
    summarize_outcomes,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.coverage import probe_symbol_coverage
from orderbook_analyse.ema_zone_microstructure_confirmation.defaults import methodology_defaults
from orderbook_analyse.ema_zone_microstructure_confirmation.regime import prepare_bars_with_ema200
from orderbook_analyse.ema_zone_microstructure_confirmation.research_layers import (
    COMPUTATION_MODE_EMA_ONLY,
    normalize_computation_mode,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.stage_a import (
    attach_direction_fields,
)
from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
from orderbook_analyse.l2_wall_attack_discovery.trades import load_public_trades
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.runner import (
    assert_live_safe,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.indicators import (
    last_closed_bar_at,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zone_replay import (
    AnalysisSample,
    replay_analysis_samples,
)
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.loaders import load_candles_1m
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings
from orderbook_analyse.strategy_lab.decoder_v2 import load_compile_candidate_discovery_v2
from orderbook_analyse.strategy_lab.validation import production_catalog_bundle_v2


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def compile_contract(repo_root: Path) -> dict[str, Any]:
    catalogs = production_catalog_bundle_v2()
    compiled = load_compile_candidate_discovery_v2(repo_root / STRATEGY_YAML, catalogs)
    return {
        "compiler": "compile_candidate_discovery_v2",
        "trade_compiler_used": False,
        "plugin_id": compiled.plugin_id,
        "strategy_hash": compiled.strategy_hash,
        "candidate_states": list(compiled.candidate_states),
    }


def _parse_z(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def make_trades_loader(symbol: str) -> Callable[[int, int], pd.DataFrame]:
    cache: dict[tuple[int, int], pd.DataFrame] = {}

    def load(start_ms: int, end_ms: int) -> pd.DataFrame:
        # round to minute buckets to improve cache hits
        a = (start_ms // 60_000) * 60_000
        b = ((end_ms + 59_999) // 60_000) * 60_000
        key = (a, b)
        if key not in cache:
            start = datetime.fromtimestamp(a / 1000.0, tz=timezone.utc)
            end = datetime.fromtimestamp(b / 1000.0, tz=timezone.utc)
            cache[key] = load_public_trades(symbol=symbol, start=start, end=end)
            # bound cache size
            if len(cache) > 40:
                cache.pop(next(iter(cache)))
        df = cache[key]
        if df.empty:
            return df
        return df[(df["ts_ms"] >= start_ms) & (df["ts_ms"] < end_ms)].copy()

    return load


def candle_analysis_samples(
    *,
    candles_1m: pd.DataFrame,
    bars_5m: pd.DataFrame,
    start: datetime,
    end: datetime,
) -> list[AnalysisSample]:
    """Stage-A touch basis from 1m OHLC — no orderbook / no L2 mid."""
    if candles_1m is None or candles_1m.empty:
        return []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    out: list[AnalysisSample] = []
    for _, row in candles_1m.iterrows():
        ts = pd.Timestamp(row["open_time"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        ts_ms = int(ts.timestamp() * 1000) + 60_000
        if ts_ms < start_ms or ts_ms >= end_ms:
            continue
        asof = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        ind = last_closed_bar_at(bars_5m, asof)
        if ind is None:
            continue
        ema20 = float(ind["ema20"]) if ind.get("ema20") is not None else None
        ema59 = float(ind["ema59"]) if ind.get("ema59") is not None else None
        atr = float(ind["atr"]) if ind.get("atr") is not None else None
        if ema20 is None or ema59 is None or atr is None or atr <= 0:
            continue
        close = float(row["close"])
        low = float(row["low"])
        high = float(row["high"])
        spread = max(close * 1e-6, 0.01)
        out.append(
            AnalysisSample(
                ts_ms=ts_ms,
                mid=close,
                best_bid=close - spread / 2,
                best_ask=close + spread / 2,
                bid_levels=200,
                ask_levels=200,
                genuine=True,
                seq_gap=False,
                carried_forward=False,
                warmup=False,
                ema20=ema20,
                ema59=ema59,
                atr=atr,
                bid_wall=None,
                ask_wall=None,
                ask_in_ema20=None,
                bid_in_ema20=None,
                ask_in_ema59=None,
                bid_in_ema59=None,
                source_file="candles_1m_ohlc",
                candle_low=low,
                candle_high=high,
            )
        )
    out.sort(key=lambda s: s.ts_ms)
    return out


def dedupe_episodes(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Episode-level rows (already one decision per episode); add sequence notes."""
    rows = []
    by_sym: dict[str, list] = {}
    for c in candidates:
        by_sym.setdefault(c["symbol"], []).append(c)
    for sym, items in by_sym.items():
        items = sorted(items, key=lambda x: x.get("zone_touch_at") or x.get("decision_at") or "")
        prev = None
        for c in items:
            parent = ""
            dedup = "independent"
            if prev and c.get("zone_name") == prev.get("zone_name"):
                # overlapping cooldown already applied in engine; mark sequential same-zone
                dedup = "same_zone_after_cooldown"
            # EMA20 -> EMA59 sequence heuristic within 45m
            if prev and "EMA20" in str(prev.get("zone_name")) and "EMA59" in str(c.get("zone_name")):
                dedup = "sequence_ema20_to_ema59"
                parent = prev["episode_id"]
            rows.append(
                {
                    "episode_id": c["episode_id"],
                    "symbol": sym,
                    "parent_episode_id": parent,
                    "sequence_id": c.get("sequence_id"),
                    "zone_name": c.get("zone_name"),
                    "candidate_state": c.get("candidate_state"),
                    "first_seen_at": c.get("zone_watch_started_at"),
                    "decision_at": c.get("decision_at"),
                    "episode_closed_at": c.get("episode_closed_at"),
                    "contact_count": 1,
                    "state_count": 3,
                    "dedup_reason": dedup,
                }
            )
            prev = c
    return rows


def sanitize_candidate_direction_fields(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Result-builder hard gate: Stage A / non-confirmed → NONE + emit=false."""
    out: list[dict[str, Any]] = []
    for c in candidates:
        row = dict(c)
        fields = attach_direction_fields(
            candidate_state=str(row.get("candidate_state") or ""),
            zone_role=str(row.get("zone_role_at_watch") or row.get("zone_role") or ""),
            raw_direction=row.get("candidate_direction"),
            direction_reason=str(row.get("direction_reason") or ""),
        )
        row.update(fields)
        out.append(row)
    return out


def run_symbol(
    *,
    symbol: str,
    raw_root: Path,
    coverage: dict[str, Any],
    smoke_hours: float | None = None,
    computation_mode: str = "ema_plus_microstructure",
) -> dict[str, Any]:
    if coverage["status"] != "OK":
        return {
            "symbol": symbol,
            "status": "DATA_INCOMPLETE",
            "coverage": coverage,
            "bundles": {},
            "quality": {
                "symbol": symbol,
                "status": "DATA_INCOMPLETE",
                "reason": coverage.get("incomplete_reason"),
            },
        }

    resolved_computation_mode = normalize_computation_mode(computation_mode)
    ema_only_computation = resolved_computation_mode == COMPUTATION_MODE_EMA_ONLY

    start = _parse_z(coverage["discovery_start"])
    end = _parse_z(coverage["discovery_end"])
    if smoke_hours is not None:
        end = min(end, start + timedelta(hours=smoke_hours))

    # candles with EMA200 warmup
    candle_start = start - timedelta(hours=240)
    load_clickhouse_settings()
    client = get_clickhouse_client()
    print(f"[{symbol}] candles {candle_start} → {end}…", flush=True)
    candles = load_candles_1m(client, symbol=symbol, start=candle_start, end=end + timedelta(hours=4))
    bars = prepare_bars_with_ema200(candles)
    print(f"[{symbol}] candles_1m={len(candles)} bars_5m={len(bars)} rss={_rss_mb():.0f}MB", flush=True)

    if ema_only_computation:
        print(f"[{symbol}] computation_mode=ema_only — skip OI/liq/trades/L2 replay", flush=True)
        oi = pd.DataFrame()
        liq = pd.DataFrame()
        samples = candle_analysis_samples(
            candles_1m=candles,
            bars_5m=bars,
            start=start,
            end=end,
        )
        genuine = [s for s in samples if s.genuine and not s.carried_forward]
        levels_ok = len(genuine)
        print(
            f"[{symbol}] candle_samples={len(samples)} genuine={len(genuine)} rss={_rss_mb():.0f}MB",
            flush=True,
        )
    else:
        print(f"[{symbol}] OI/liq…", flush=True)
        oi = fetch_oi_1m(client, symbol, start - timedelta(hours=1), end + timedelta(minutes=5))
        liq = fetch_liquidations(client, symbol, start - timedelta(hours=1), end + timedelta(minutes=5))

        print(f"[{symbol}] L2 replay {start} → {end}…", flush=True)
        samples = replay_analysis_samples(
            raw_root, symbol=symbol, start=start, end=end, bars_5m=bars, sample_ms=SAMPLE_MS
        )
        genuine = [s for s in samples if s.genuine and not s.carried_forward]
        levels_ok = sum(1 for s in genuine if s.bid_levels >= 200 and s.ask_levels >= 200)
        print(
            f"[{symbol}] samples={len(samples)} genuine={len(genuine)} levels200={levels_ok} rss={_rss_mb():.0f}MB",
            flush=True,
        )
    if _rss_mb() > 12_000:
        raise SystemExit(f"RESOURCE_BLOCKED: RSS {_rss_mb():.0f}MB too high before detect")

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    tick = tick_size(symbol)
    if ema_only_computation:
        trades_loader: Callable[..., pd.DataFrame] = lambda _a, _b: pd.DataFrame()
    else:
        trades_loader = make_trades_loader(symbol)

    print(f"[{symbol}] autonomous detect ({resolved_computation_mode})…", flush=True)
    bundles = process_symbol_stream(
        symbol=symbol,
        samples=samples,
        bars=bars,
        trades_loader=trades_loader,
        oi=oi,
        liq=liq,
        tick=tick,
        discovery_start_ms=start_ms,
        discovery_end_ms=end_ms,
        computation_mode=resolved_computation_mode,
    )
    print(
        f"[{symbol}] candidates={len(bundles['candidate_events'])} watches={len(bundles['zone_watch_events'])} rss={_rss_mb():.0f}MB",
        flush=True,
    )
    bundles["candidate_events"] = sanitize_candidate_direction_fields(
        bundles["candidate_events"]
    )

    path_end = end + timedelta(hours=4)
    if not candles.empty:
        cmax = pd.Timestamp(candles["open_time"].max())
        if cmax.tzinfo is None:
            cmax = cmax.tz_localize("UTC")
        else:
            cmax = cmax.tz_convert("UTC")
        path_end_ms = int(cmax.timestamp() * 1000) + 60_000
    else:
        path_end_ms = int(path_end.timestamp() * 1000)

    path = build_price_path(samples, candles, start_ms=start_ms, end_ms=path_end_ms)
    outcomes = label_outcomes_for_candidates(
        bundles["candidate_events"], path=path, path_end_ms=path_end_ms
    )
    controls = build_controls(bundles["candidate_events"])
    episodes = dedupe_episodes(bundles["candidate_events"])

    quality = {
        "symbol": symbol,
        "status": "OK",
        "l2_samples": len(samples),
        "l2_genuine": len(genuine),
        "l2_levels_200": levels_ok,
        "l2_carried_forward": sum(1 for s in samples if s.carried_forward),
        "mean_sample_ms": SAMPLE_MS,
        "candles_1m": len(candles),
        "oi_rows": len(oi),
        "liq_rows": len(liq),
        "data_basis": "candles_1m" if ema_only_computation else "orderbook_ob200_v3_raw",
        "touch_price_basis": "candle_ohlc_1m" if ema_only_computation else "l2_mid",
        "orderbook_loaded": not ema_only_computation,
        "discovery_start": start.isoformat().replace("+00:00", "Z"),
        "discovery_end": end.isoformat().replace("+00:00", "Z"),
        "computation_mode": resolved_computation_mode,
        "rss_mb_peak_approx": _rss_mb(),
        "open_tmp_ignored": coverage.get("open_tmp_ignored"),
    }
    return {
        "symbol": symbol,
        "status": "OK",
        "coverage": coverage,
        "bundles": bundles,
        "outcomes": outcomes,
        "controls": controls,
        "episodes": episodes,
        "quality": quality,
        "path_end_ms": path_end_ms,
    }


def _funnel(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    df = pd.DataFrame(candidates)
    rows = []
    for (sym, state), g in df.groupby(["symbol", "candidate_state"]):
        rows.append({"symbol": sym, "candidate_state": state, "n_events": int(len(g)), "n_episodes": int(g["episode_id"].nunique())})
    return rows


def write_report(out_dir: Path, *, manifest: dict, results: list[dict]) -> None:
    lines = [
        "# EZM Continuous Discovery v1",
        "",
        f"- Format: `{FORMAT_VERSION}`",
        f"- Compiler: compile_candidate_discovery_v2 (trade compiler unused)",
        f"- Manual windows: NOT used as centers",
        "",
        "## Per symbol",
        "",
    ]
    for r in results:
        cands = r.get("bundles", {}).get("candidate_events", [])
        lines.append(
            f"- **{r['symbol']}**: status={r['status']} candidates={len(cands)} "
            f"watches={len(r.get('bundles', {}).get('zone_watch_events', []))} "
            f"contacts={len(r.get('bundles', {}).get('zone_contacts', []))}"
        )
        by_state: dict[str, int] = {}
        for c in cands:
            by_state[c["candidate_state"]] = by_state.get(c["candidate_state"], 0) + 1
        for st, n in sorted(by_state.items()):
            lines.append(f"  - {st}: {n}")
    lines += ["", "## Kernfragen", ""]
    lines.append("- Autonome Events ohne manuelle Fenster: siehe candidate_events.csv")
    lines.append("- Outcomes nur Labels nach label_anchor_at — kein Trade/PnL")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(
    *,
    repo_root: Path,
    raw_root: Path,
    out_root: Path,
    symbols: tuple[str, ...] = SYMBOLS_DEFAULT,
    smoke_hours: float | None = None,
    smoke_symbol: str | None = None,
    out_subdir: str | None = None,
) -> Path:
    out_dir = out_root / (out_subdir or OUT_SUBDIR)
    if out_dir.exists():
        raise SystemExit(f"NO_OVERWRITE: {out_dir} already exists")
    out_dir.mkdir(parents=True, exist_ok=False)

    live = assert_live_safe()
    contract = compile_contract(repo_root)

    coverage_rows = []
    coverages: dict[str, dict] = {}
    for sym in symbols:
        cov = probe_symbol_coverage(symbol=sym, raw_root=raw_root)
        coverages[sym] = cov
        coverage_rows.append(
            {
                "symbol": sym,
                "status": cov["status"],
                "intersection_start": cov.get("intersection_start"),
                "intersection_end": cov.get("intersection_end"),
                "closed_segments": cov.get("closed_segments"),
                "open_tmp_ignored": cov.get("open_tmp_ignored"),
                "incomplete_reason": cov.get("incomplete_reason"),
                "outcome_path_end": cov.get("outcome_path_end"),
            }
        )
        # flatten source spans
        for src in cov.get("sources", []):
            coverage_rows.append(
                {
                    "symbol": sym,
                    "status": src.get("status"),
                    "source": src.get("source"),
                    "min_ts": src.get("min_ts"),
                    "max_ts": src.get("max_ts"),
                    "n_rows": src.get("n_rows"),
                    "notes": src.get("notes"),
                }
            )

    pd.DataFrame(coverage_rows).to_csv(out_dir / "coverage_by_symbol.csv", index=False)

    run_syms = [smoke_symbol] if smoke_symbol else list(symbols)
    if smoke_hours is not None and smoke_symbol is None:
        run_syms = [symbols[0]]

    results = []
    for sym in run_syms:
        if coverages[sym]["status"] != "OK":
            print(f"[{sym}] DATA_INCOMPLETE — skip full detect", flush=True)
            results.append(
                {
                    "symbol": sym,
                    "status": "DATA_INCOMPLETE",
                    "coverage": coverages[sym],
                    "bundles": {
                        "regime_timeline": [],
                        "zone_watch_events": [],
                        "zone_contacts": [],
                        "episode_timeline": [],
                        "wall_evidence": [],
                        "public_trade_evidence": [],
                        "oi_liquidation_evidence": [],
                        "candidate_events": [],
                        "ema_setup_events": [],
                        "microstructure_confirmation_events": [],
                    },
                    "outcomes": [],
                    "controls": [],
                    "episodes": [],
                    "quality": {"symbol": sym, "status": "DATA_INCOMPLETE"},
                }
            )
            continue
        results.append(
            run_symbol(
                symbol=sym,
                raw_root=raw_root,
                coverage=coverages[sym],
                smoke_hours=smoke_hours,
            )
        )
        # encourage GC between symbols
        import gc

        gc.collect()
        print(f"[{sym}] post-run rss={_rss_mb():.0f}MB", flush=True)

    # Aggregate artifacts
    def cat(key: str) -> list:
        out = []
        for r in results:
            out.extend(r.get("bundles", {}).get(key, []))
        return out

    all_cands = sanitize_candidate_direction_fields(cat("candidate_events"))
    all_outcomes = [o for r in results for o in r.get("outcomes", [])]
    all_controls = [c for r in results for c in r.get("controls", [])]
    all_eps = [e for r in results for e in r.get("episodes", [])]
    quality = [r.get("quality", {}) for r in results]

    pd.DataFrame(cat("regime_timeline")).to_csv(out_dir / "regime_timeline.csv", index=False)
    pd.DataFrame(cat("zone_watch_events")).to_csv(out_dir / "zone_watch_events.csv", index=False)
    pd.DataFrame(cat("zone_contacts")).to_csv(out_dir / "zone_contacts.csv", index=False)
    pd.DataFrame(cat("ema_setup_events")).to_csv(out_dir / "ema_setup_events.csv", index=False)
    pd.DataFrame(cat("microstructure_confirmation_events")).to_csv(
        out_dir / "microstructure_confirmation_events.csv", index=False
    )
    pd.DataFrame(cat("episode_timeline")).to_csv(out_dir / "episode_timeline.csv", index=False)
    pd.DataFrame(cat("wall_evidence")).to_csv(out_dir / "wall_evidence.csv", index=False)
    pd.DataFrame(cat("public_trade_evidence")).to_csv(out_dir / "public_trade_evidence.csv", index=False)
    pd.DataFrame(cat("oi_liquidation_evidence")).to_csv(out_dir / "oi_liquidation_evidence.csv", index=False)
    pd.DataFrame(all_cands).to_csv(out_dir / "candidate_events.csv", index=False)
    pd.DataFrame(_funnel(all_cands)).to_csv(out_dir / "candidate_state_funnel.csv", index=False)
    pd.DataFrame(all_eps).to_csv(out_dir / "deduplicated_episodes.csv", index=False)
    pd.DataFrame(all_outcomes).to_csv(out_dir / "directional_outcomes.csv", index=False)
    pd.DataFrame(summarize_outcomes(all_outcomes, ["candidate_state"])).to_csv(
        out_dir / "outcome_summary_by_state.csv", index=False
    )
    pd.DataFrame(summarize_outcomes(all_outcomes, ["candidate_direction"])).to_csv(
        out_dir / "outcome_summary_by_direction.csv", index=False
    )
    pd.DataFrame(summarize_outcomes(all_outcomes, ["regime"])).to_csv(
        out_dir / "outcome_summary_by_regime.csv", index=False
    )
    pd.DataFrame(summarize_outcomes(all_outcomes, ["zone_name"])).to_csv(
        out_dir / "outcome_summary_by_zone.csv", index=False
    )
    pd.DataFrame(summarize_outcomes(all_outcomes, ["mechanism"])).to_csv(
        out_dir / "outcome_summary_by_mechanism.csv", index=False
    )
    pd.DataFrame(all_controls).to_csv(out_dir / "controls.csv", index=False)
    pd.DataFrame(matched_control_summary(all_controls)).to_csv(
        out_dir / "matched_control_summary.csv", index=False
    )
    pd.DataFrame(quality).to_csv(out_dir / "data_quality.csv", index=False)

    methodology = {
        "format_version": FORMAT_VERSION,
        "pipeline": "autonomous_continuous_discovery",
        "manual_windows_used": False,
        "defaults": {**methodology_defaults(), **continuous_research_defaults()},
        "contract": contract,
        "no_trade_simulation": True,
        "oi_liq_hard_gates": False,
    }
    (out_dir / "methodology.json").write_text(json.dumps(methodology, indent=2), encoding="utf-8")

    git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    git_branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "format_version": FORMAT_VERSION,
        "git_branch": git_branch,
        "git_head": git_head,
        "live_safety": live,
        "contract": contract,
        "symbols": list(run_syms),
        "smoke_hours": smoke_hours,
        "n_candidates": len(all_cands),
        "rss_mb": _rss_mb(),
        "artifacts_exclude": ["trades", "orders", "positions", "pnl"],
        "no_overwrite": True,
        "manual_artifacts_untouched": True,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    write_report(out_dir, manifest=manifest, results=results)
    print(f"DONE → {out_dir} candidates={len(all_cands)} rss={_rss_mb():.0f}MB", flush=True)
    return out_dir
