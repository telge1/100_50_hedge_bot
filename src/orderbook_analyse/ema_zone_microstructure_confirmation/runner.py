"""EZM candidate-discovery runner (research / read-only).

Uses compile_candidate_discovery_v2 — never the trade compiler.
Reuses manual_ema_wall_windows + loaders; no second wall/EMA semantics.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
    fetch_liquidations,
    fetch_oi_1m,
)
from orderbook_analyse.ema_zone_microstructure_confirmation import (
    FORMAT_VERSION,
    OUT_SUBDIR,
    PLUGIN_ID,
    STRATEGY_YAML,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.candidate_states import (
    build_state_timeline,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.defaults import (
    REGISTERED_CANDIDATE_STATES,
    methodology_defaults,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.manual_compare import compare_window
from orderbook_analyse.ema_zone_microstructure_confirmation.oi_liq import (
    liquidation_features,
    oi_features,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.regime import (
    detect_ema200_flip_timestamps,
    prepare_bars_with_ema200,
    regime_snapshot,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.zones_ext import (
    build_zones,
    zone_feature_row,
)
from orderbook_analyse.l2_wall_attack_discovery.trades import load_public_trades
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import (
    MISSING,
    SYMBOL,
    TICK,
    WINDOWS,
    ZONE_ATR_FRAC,
    ZONE_MIN_TICKS,
    parse_utc,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.episodes import (
    dedupe_episodes,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.indicators import (
    find_swings,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.runner import (
    analyze_one_window,
    assert_live_safe,
    coverage_for_window,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zone_replay import (
    replay_analysis_samples,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import (
    swing_in_zone,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.loaders import load_candles_1m
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings
from orderbook_analyse.strategy_lab.decoder_v2 import load_compile_candidate_discovery_v2
from orderbook_analyse.strategy_lab.validation import production_catalog_bundle_v2


def compile_ezm_contract(repo_root: Path) -> dict[str, Any]:
    """Validate StrategySpec via candidate compiler (not trade compile)."""
    catalogs = production_catalog_bundle_v2()
    yaml_path = repo_root / STRATEGY_YAML
    compiled = load_compile_candidate_discovery_v2(yaml_path, catalogs)
    states = tuple(compiled.candidate_states)
    if set(states) != set(REGISTERED_CANDIDATE_STATES):
        raise RuntimeError(
            f"candidate_states mismatch: compiled={states} expected={REGISTERED_CANDIDATE_STATES}"
        )
    if compiled.plugin_id != PLUGIN_ID:
        raise RuntimeError(f"plugin_id mismatch: {compiled.plugin_id}")
    return {
        "strategy_hash": compiled.strategy_hash,
        "plugin_id": compiled.plugin_id,
        "candidate_states": list(states),
        "data_requirement_ids": list(compiled.data_requirement_ids),
        "compiler": "compile_candidate_discovery_v2",
        "trade_compiler_used": False,
    }


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def enrich_window(
    *,
    window: dict[str, str],
    base: dict[str, Any],
    bars: pd.DataFrame,
    samples: list[Any],
    oi: pd.DataFrame,
    liq: pd.DataFrame,
    cov: dict[str, Any],
) -> dict[str, Any]:
    center = parse_utc(window["center_utc"])
    start = parse_utc(window["start_utc"])
    end = parse_utc(window["end_utc"])
    reg = regime_snapshot(bars, center)
    swings = find_swings(bars, center)

    zones = build_zones(
        ema20=reg["ema20"],
        ema59=reg["ema59"],
        ema200=reg["ema200"] if reg.get("ema200_warmup_ok") else None,
        atr=reg["atr"],
    )
    primary_name = base["classification"]["primary_zone"]
    if primary_name in (None, "none", MISSING):
        primary_name = "EMA20"
    contact_at = base["timeline"].get("zone_touch_at")
    contact_at = None if contact_at in (None, MISSING) else contact_at
    contact_ts = base["confluence"].get("contact_ts_ms")
    contact_ts_ms = None if contact_ts in (None, MISSING) else int(contact_ts)

    # Approach: mid ~30s before contact
    mid_before = None
    mid_at = None
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    win_samples = [s for s in samples if start_ms <= s.ts_ms < end_ms]
    if contact_ts_ms is not None:
        pre = [s for s in win_samples if contact_ts_ms - 30_000 <= s.ts_ms < contact_ts_ms]
        if pre:
            mid_before = pre[0].mid
        at = [s for s in win_samples if s.ts_ms >= contact_ts_ms]
        if at:
            mid_at = at[0].mid
    elif win_samples:
        mid_at = min(win_samples, key=lambda s: abs(s.ts_ms - int(center.timestamp() * 1000))).mid

    wall_conf = bool(base["confluence"].get("wall_majorish_at"))
    sh = swings.get("last_swing_high")
    sl = swings.get("last_swing_low")
    sh_f = float(sh) if sh not in (None, MISSING) else None
    sl_f = float(sl) if sl not in (None, MISSING) else None
    pz = zones.get(primary_name)
    swing_conf = bool(pz and ((sh_f and swing_in_zone(sh_f, pz)) or (sl_f and swing_in_zone(sl_f, pz))))

    watch_start = window["start_utc"]
    if mid_before is not None and pz is not None and contact_ts_ms is not None:
        # first sample approaching within 3 half-widths
        for s in win_samples:
            dist = 0.0 if pz.low <= s.mid <= pz.high else (
                pz.low - s.mid if s.mid < pz.low else s.mid - pz.high
            )
            if dist <= pz.half_width * 3:
                watch_start = _iso(s.ts_ms)
                break

    zone_row = zone_feature_row(
        window_id=window["window_id"],
        zones=zones,
        mid=mid_at,
        mid_before=mid_before,
        primary_name=primary_name,
        wall_confluence=wall_conf,
        swing_confluence=swing_conf,
        zone_watch_started_at=watch_start,
        zone_touch_at=contact_at,
    )

    # Zone contact row
    contact_row = {
        "window_id": window["window_id"],
        "zone_name": primary_name,
        "zone_role": base["classification"]["zone_role"],
        "approach_side": zone_row["approach_side"],
        "zone_watch_started_at": zone_row["zone_watch_started_at"],
        "zone_touch_at": zone_row["zone_touch_at"],
        "band_low": pz.low if pz else MISSING,
        "band_high": pz.high if pz else MISSING,
        "dist_pct": zone_row.get(f"{primary_name.lower()}_dist_pct", MISSING),
        "dist_ticks": zone_row.get(f"{primary_name.lower()}_dist_ticks", MISSING),
        "dist_atr": zone_row.get(f"{primary_name.lower()}_dist_atr", MISSING),
        "nearest_stronger_zone": zone_row.get("nearest_stronger_zone", MISSING),
        "clearance_pct": zone_row.get("clearance_pct", MISSING),
        "wait_next_zone": zone_row.get("wait_next_zone", False),
        "stacked_ema_zone": zone_row.get("stacked_ema_zone", ""),
    }

    flip = detect_ema200_flip_timestamps(
        bars=bars,
        samples=win_samples,
        zone200_low=zones["EMA200"].low if zones.get("EMA200") else None,
        zone200_high=zones["EMA200"].high if zones.get("EMA200") else None,
        role=base["classification"]["zone_role"],
        mechanism=base["classification"]["mechanism"],
        timeline=base["timeline"],
        contact_ts_ms=contact_ts_ms,
    )

    contact_dt = parse_utc(contact_at) if contact_at else None
    price_after = None
    if contact_ts_ms is not None:
        post = [s for s in win_samples if contact_ts_ms + 60_000 <= s.ts_ms <= contact_ts_ms + 120_000]
        if post:
            price_after = post[-1].mid
    oi_row = oi_features(
        oi,
        window_id=window["window_id"],
        contact_at=contact_dt or center,
        price_before=mid_before,
        price_after=price_after or mid_at,
    )
    liq_row = liquidation_features(
        liq,
        window_id=window["window_id"],
        start=start,
        end=end,
        contact_at=contact_dt or center,
    )
    oi_liq_row = {**oi_row, **{k: v for k, v in liq_row.items() if k != "window_id"}}

    data_incomplete = cov["status"] == "DATA_INCOMPLETE"
    quality = cov["status"]
    # Decision evidence ends at classification_at (or last window sample) — never global L2 day-end.
    class_at = base["timeline"].get("classification_at")
    if class_at not in (None, MISSING):
        evidence_until = str(class_at)
    elif win_samples:
        evidence_until = _iso(win_samples[-1].ts_ms)
    else:
        evidence_until = window["end_utc"]

    timeline_rows, final_state, reason_codes = build_state_timeline(
        window_id=window["window_id"],
        window_start=window["start_utc"],
        contact_at=contact_at,
        classification_at=None
        if base["timeline"]["classification_at"] in (None, MISSING)
        else base["timeline"]["classification_at"],
        data_incomplete=data_incomplete,
        incomplete_reason=cov.get("incomplete_reason", ""),
        block_flat=bool(reg["block_flat_compression"]),
        wait_next_zone=bool(zone_row.get("wait_next_zone")),
        primary_class=base["classification"]["primary_class"],
        mechanism=base["classification"]["mechanism"],
        possible_regime_flip=bool(flip["possible_regime_flip"]),
        full_regime_flip=bool(flip["full_regime_flip_confirmed"]),
        flip_clocks=flip,
        evidence_until=evidence_until,
        quality_status=quality,
    )

    # Wall evidence (reuse confluence + lifecycle; tag liquidity pull)
    wall_ev = {
        **base["confluence"],
        **{f"lifecycle_{k}": v for k, v in base["lifecycle"].items() if k != "window_id"},
        "liquidity_pull_not_absorption": base["classification"]["mechanism"] == "LIQUIDITY_PULL",
        "is_majorish_helper": True,
    }

    event_row = {
        "window_id": window["window_id"],
        "start_utc": window["start_utc"],
        "end_utc": window["end_utc"],
        "center_utc": window["center_utc"],
        "candidate_state": final_state,
        "reason_codes": "|".join(reason_codes),
        "regime": reg["regime"],
        "primary_zone": primary_name,
        "zone_kind": zone_row["zone_kind"],
        "mechanism": base["classification"]["mechanism"],
        "primary_class": base["classification"]["primary_class"],
        "data_coverage": cov["status"],
        "trade_execution": False,
        **{f"flip_{k}": v for k, v in flip.items()},
    }

    regime_row = {
        "window_id": window["window_id"],
        "center_utc": window["center_utc"],
        **{k: (MISSING if v is None else v) for k, v in reg.items()},
    }

    compare = compare_window(
        window_id=window["window_id"],
        candidate_state=final_state,
        primary_zone=primary_name,
        mechanism=base["classification"]["mechanism"],
        primary_class=base["classification"]["primary_class"],
        regime=reg["regime"],
        data_coverage=cov["status"],
    )

    # public trade evidence = impact intervals from base
    return {
        "regime": regime_row,
        "ema_zone": zone_row,
        "zone_contact": contact_row,
        "wall_evidence": wall_ev,
        "public_trade_evidence": base["impacts"],
        "oi_liquidation_evidence": oi_liq_row,
        "candidate_state_timeline": timeline_rows,
        "candidate_event": event_row,
        "manual_compare": compare,
        "summary": {
            **window,
            "primary_class": base["classification"]["primary_class"],
            "primary_zone": primary_name,
            "zone_role": base["classification"]["zone_role"],
            "primary_wall_price": base["classification"].get("primary_wall_price", MISSING),
            "candidate_state": final_state,
        },
        "_base": base,
    }


def write_report(out_dir: Path, *, manifest: dict, results: list, cov: list) -> None:
    lines = [
        "# EMA Zone Microstructure Confirmation — BTC manual windows v1",
        "",
        f"- Format: `{FORMAT_VERSION}`",
        f"- Output: `{out_dir}`",
        f"- Compiler: `compile_candidate_discovery_v2` (trade compiler unused)",
        f"- Live safety: collectors untouched; CH read-only; closed raw only; no overwrite",
        "",
        "## Candidate states by window",
        "",
    ]
    for r in results:
        e = r["candidate_event"]
        lines.append(
            f"- **{e['window_id']}**: `{e['candidate_state']}` | regime={e['regime']} | "
            f"zone={e['primary_zone']} | mech={e['mechanism']} | class={e['primary_class']} | "
            f"coverage={e['data_coverage']}"
        )
    lines += ["", "## Manual parity", ""]
    for r in results:
        c = r["manual_compare"]
        lines.append(f"- **{c['window_id']}**: {c['parity']} — {c['notes']}")
    lines += ["", "## Coverage", ""]
    for c in cov:
        lines.append(
            f"- {c['window_id']}: {c['status']} (L2 genuine={c['l2_genuine']}, trades={c['trade_count']})"
        )
    lines += [
        "",
        "## Scope reminder",
        "",
        "No entry/exit/TP/SL/size/portfolio/PnL. Outcomes do not feed candidate states.",
        "",
    ]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(
    *,
    repo_root: Path,
    raw_root: Path,
    out_root: Path,
    force: bool = False,
) -> Path:
    out_dir = out_root / OUT_SUBDIR
    if out_dir.exists() and not force:
        raise SystemExit(f"NO_OVERWRITE: output folder already exists: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=False)

    live = assert_live_safe()
    contract = compile_ezm_contract(repo_root)

    load_clickhouse_settings()
    client = get_clickhouse_client()

    candle_start = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    candle_end = datetime(2026, 8, 25, 15, 10, tzinfo=timezone.utc)
    print("Loading candles…", flush=True)
    candles = load_candles_1m(client, symbol=SYMBOL, start=candle_start, end=candle_end)
    bars = prepare_bars_with_ema200(candles)
    print(f"  candles_1m={len(candles)} bars_5m={len(bars)}", flush=True)

    trade_start = datetime(2026, 8, 25, 7, 45, tzinfo=timezone.utc)
    trade_end = datetime(2026, 8, 25, 15, 10, tzinfo=timezone.utc)
    print("Loading native public trades…", flush=True)
    trades = load_public_trades(symbol=SYMBOL, start=trade_start, end=trade_end)
    print(f"  trades={len(trades)}", flush=True)

    print("Loading OI 1m + liquidations…", flush=True)
    oi = fetch_oi_1m(client, SYMBOL, trade_start, trade_end)
    liq = fetch_liquidations(client, SYMBOL, trade_start, trade_end)
    print(f"  oi_1m={len(oi)} liquidations={len(liq)}", flush=True)

    l2_start = datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc)
    l2_end = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
    segs = list_closed_segments(
        raw_root, symbols=(SYMBOL,), start=l2_start, end=l2_end, include_boundary_stubs=False
    )
    closed_l2_end = max((s.end_utc for s in segs), default=l2_end)
    print(f"  closed segments={len(segs)} closed_l2_end={closed_l2_end}", flush=True)

    print("Replaying L2 samples (closed raw only)…", flush=True)
    samples = replay_analysis_samples(
        raw_root,
        symbol=SYMBOL,
        start=l2_start,
        end=l2_end,
        bars_5m=bars,
    )
    print(f"  samples={len(samples)}", flush=True)

    cov_rows = [
        coverage_for_window(
            window=w,
            samples=samples,
            trades=trades,
            candles_1m=candles,
            closed_l2_end=closed_l2_end,
        )
        for w in WINDOWS
    ]

    results: list[dict[str, Any]] = []
    for w, cov in zip(WINDOWS, cov_rows):
        print(f"Analyzing {w['window_id']}…", flush=True)
        base = analyze_one_window(
            window=w, samples=samples, trades=trades, bars_5m=bars, cov=cov
        )
        # Drop outcome influence on state: do not pass outcomes into enrich
        enriched = enrich_window(
            window=w,
            base=base,
            bars=bars,
            samples=samples,
            oi=oi,
            liq=liq,
            cov=cov,
        )
        # Explicitly ignore base outcomes for candidate state
        _ = base.get("outcomes")
        results.append(enriched)

    # --- Artifacts (no trades / orders / PnL files) ---
    pd.DataFrame(cov_rows).to_csv(out_dir / "data_coverage.csv", index=False)
    pd.DataFrame([r["regime"] for r in results]).to_csv(out_dir / "regime_timeline.csv", index=False)
    pd.DataFrame([r["ema_zone"] for r in results]).to_csv(out_dir / "ema_zones.csv", index=False)
    pd.DataFrame([r["zone_contact"] for r in results]).to_csv(out_dir / "zone_contacts.csv", index=False)
    pd.DataFrame([r["wall_evidence"] for r in results]).to_csv(out_dir / "wall_evidence.csv", index=False)
    pt_all = [row for r in results for row in r["public_trade_evidence"]]
    pd.DataFrame(pt_all).to_csv(out_dir / "public_trade_evidence.csv", index=False)
    pd.DataFrame([r["oi_liquidation_evidence"] for r in results]).to_csv(
        out_dir / "oi_liquidation_evidence.csv", index=False
    )
    tl_all = [row for r in results for row in r["candidate_state_timeline"]]
    pd.DataFrame(tl_all).to_csv(out_dir / "candidate_state_timeline.csv", index=False)
    pd.DataFrame([r["candidate_event"] for r in results]).to_csv(
        out_dir / "candidate_events.csv", index=False
    )
    episodes = dedupe_episodes([r["summary"] for r in results])
    pd.DataFrame(episodes).to_csv(out_dir / "deduplicated_episodes.csv", index=False)
    pd.DataFrame([r["manual_compare"] for r in results]).to_csv(
        out_dir / "manual_window_comparison.csv", index=False
    )

    methodology = {
        "format_version": FORMAT_VERSION,
        "symbol": SYMBOL,
        "timezone": "UTC",
        "pipeline": "regime -> ema_zone -> microstructure -> candidate_state",
        "reuse": [
            "manual_ema_wall_windows.indicators/zones/classify/impact/zone_replay",
            "l2_wall_attack_discovery.trades.load_public_trades",
            "ob200_v3_raw_discovery.files.list_closed_segments",
            "cluster_sweep_research.clickhouse_source.fetch_oi_1m/fetch_liquidations",
            "oi_liq_impact_l2.aggregate_proxy.loaders.load_candles_1m",
        ],
        "defaults": methodology_defaults(),
        "zone_half_width_formula": f"max({ZONE_ATR_FRAC}*ATR, {ZONE_MIN_TICKS}*{TICK})",
        "closed_5m_only": True,
        "raw_ob200_not_replaced_by_1m": True,
        "native_trades_not_replaced_by_1m": True,
        "no_interpolation": True,
        "carried_forward_not_genuine": True,
        "oi_liq_hard_gates": False,
        "trade_execution": False,
        "contract": contract,
    }
    (out_dir / "methodology.json").write_text(json.dumps(methodology, indent=2), encoding="utf-8")

    git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    git_branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
    ).strip()
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "format_version": FORMAT_VERSION,
        "git_branch": git_branch,
        "git_head": git_head,
        "live_safety": live,
        "contract": contract,
        "n_samples": len(samples),
        "n_trades": len(trades),
        "n_candles_1m": len(candles),
        "n_oi": len(oi),
        "n_liquidations": len(liq),
        "closed_l2_end": closed_l2_end.isoformat().replace("+00:00", "Z"),
        "windows": WINDOWS,
        "artifacts_exclude": ["trades", "orders", "pnl"],
        "no_overwrite": True,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    write_report(out_dir, manifest=manifest, results=results, cov=cov_rows)
    print(f"DONE → {out_dir}", flush=True)
    return out_dir
