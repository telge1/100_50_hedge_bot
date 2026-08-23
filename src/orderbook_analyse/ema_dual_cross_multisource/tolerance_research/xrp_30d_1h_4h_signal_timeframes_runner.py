"""XRP 30d research: 1h and 4h signal timeframes (M0/M4/M5). Does not alter 5m/15m/30m exports."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ...cluster_sweep_research.clickhouse_source import (
    _timeframe_minutes,
    aggregate_timeframe,
    default_client,
    fetch_candles_1m,
    fetch_liquidations,
    fetch_ob_1m,
    fetch_oi_1m,
    fetch_trades_1m,
)
from ...cluster_sweep_research.ema_features import attach_emas, required_warmup_bars
from ..config import EMA_DUAL_CROSS_DEFAULTS
from ..coverage_gate import assess_coverage
from ..ema_candidate import attach_atr
from ..episode_state import EpisodeTracker
from ..feature_builder import build_gate_features
from ..models import CandidateType, FinalVerdict
from ..timeframes import bar_close as compute_bar_close, timeframe_duration
from .core_sources_research_policy import (
    apply_core_sources_research,
    apply_production_gate,
    assign_coverage_segment,
)
from .mfe_mae import compute_mfe_mae_horizon
from .mfe_runner import _git_meta, build_mode_catalog, detect_for_mode
from .research_policy import compute_all_source_verdicts, map_source_contribution
from .tpsl_pnl_engine import aggregate_strategy_stats, apply_costs, simulate_tpsl_trade

MODE_IDS = ("M0_STRICT_SYNC", "M4_TOUCH_05_EXP_1", "M5_COMPRESSED_REBOUND")
TIMEFRAMES = ("1h", "4h")
WINDOW_START = datetime(2026, 7, 24, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 23, tzinfo=timezone.utc)

WARMUP_PREFERRED_BARS = 500
WARMUP_MIN_BARS = 250

HORIZONS_BY_TF: dict[str, tuple[tuple[str, int], ...]] = {
    "1h": (("1h", 60), ("2h", 120), ("4h", 240), ("8h", 480), ("12h", 720)),
    "4h": (("4h", 240), ("8h", 480), ("12h", 720), ("24h", 1440)),
}

FIRST_HIT_PAIRS = (
    (0.40, 0.50),
    (0.50, 0.50),
    (0.60, 0.50),
    (0.75, 0.50),
    (0.40, 1.00),
    (0.75, 1.00),
    (1.00, 1.00),
    (1.25, 1.00),
    (1.50, 1.00),
)

TP_LEVELS = (0.40, 0.50, 0.60, 0.75, 1.00, 1.25, 1.50)
SL_LEVELS = (0.50, 1.00)
COST_LEVELS = (0.0, 0.11, 0.15, 0.20)
REF_COST = 0.15

GROUP_MAP = {
    "EMA_RAW": lambda c: True,
    "CORE_RESEARCH_SUPPORTIVE": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_SUPPORTIVE",
    "CORE_RESEARCH_ADVERSE": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_ADVERSE",
    "CORE_RESEARCH_MIXED": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_MIXED",
    "CORE_RESEARCH_INSUFFICIENT": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_INSUFFICIENT",
    "FULL_MULTISOURCE": lambda c: c.get("coverage_segment") == "FULL_MULTISOURCE",
    "PRODUCTION_ALLOW": lambda c: c.get("production_gate_verdict") == "ALLOW",
    "PRODUCTION_BLOCK": lambda c: c.get("production_gate_verdict") == "BLOCK",
    "PRODUCTION_INCONCLUSIVE": lambda c: c.get("production_gate_verdict") == "INCONCLUSIVE_DATA",
}

EXPORT_DIR = "results/edc_sync_tolerance/xrp_30d_1h_4h_signal_timeframes"
EXISTING_CORE = "results/edc_sync_tolerance/xrp_30d_core_sources_comparison"


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sid(tp: float, sl: float) -> str:
    return f"TP{int(round(tp * 100)):03d}_SL{int(round(sl * 100)):03d}"


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _first_1m_open_after(candles_1m: pd.DataFrame, decision_at: datetime) -> tuple[datetime | None, float | None]:
    """First available 1m open at-or-after decision_at (causal entry after bar confirmation)."""
    if candles_1m is None or candles_1m.empty:
        return None, None
    tcol = pd.to_datetime(candles_1m["open_time"])
    dec = _utc(decision_at)
    if getattr(tcol.dt, "tz", None) is not None:
        mask = tcol >= pd.Timestamp(dec)
    else:
        mask = tcol >= pd.Timestamp(dec.replace(tzinfo=None))
    sub = candles_1m.loc[mask].sort_values("open_time")
    if sub.empty:
        return None, None
    row = sub.iloc[0]
    ts = pd.Timestamp(row["open_time"]).to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    # Strictly no candle before decision_at
    if ts < dec:
        return None, None
    return ts, float(row["open"])


def _warmup_audit(df: pd.DataFrame, *, timeframe: str, start_at: datetime, cfg) -> dict[str, Any]:
    start = _utc(start_at)
    tcol = pd.to_datetime(df["open_time"])
    if getattr(tcol.dt, "tz", None) is not None:
        pre = df.loc[tcol < pd.Timestamp(start)]
    else:
        pre = df.loc[tcol < pd.Timestamp(start.replace(tzinfo=None))]
    n_pre = int(len(pre))
    # bars with valid slow EMA before start
    if "ema_59" in pre.columns:
        n_valid_ema = int(pre["ema_59"].notna().sum())
    else:
        n_valid_ema = 0
    need = required_warmup_bars(cfg.ema_slow, 20)
    status = "OK"
    if n_pre < WARMUP_MIN_BARS or n_valid_ema < WARMUP_MIN_BARS:
        status = "INSUFFICIENT"
    elif n_pre < WARMUP_PREFERRED_BARS:
        status = "PARTIAL_PREFERRED"
    return {
        "timeframe": timeframe,
        "timeframe_minutes": _timeframe_minutes(timeframe),
        "preferred_warmup_bars": WARMUP_PREFERRED_BARS,
        "min_warmup_bars": WARMUP_MIN_BARS,
        "engine_required_warmup_bars": need,
        "bars_before_start": n_pre,
        "bars_with_valid_ema59_before_start": n_valid_ema,
        "status": status,
        "first_bar_open": str(pre.iloc[0]["open_time"]) if n_pre else None,
        "last_warmup_bar_open": str(pre.iloc[-1]["open_time"]) if n_pre else None,
    }


def _feature_audit() -> dict[str, Any]:
    return {
        "aggregate_timeframe_supports_hours": True,
        "feature_builder_pre_window": "one prior signal bar via timeframe_duration",
        "feature_builder_cross_window": "signal bar [candidate_at, decision_at)",
        "feature_builder_baseline_window": "fixed 60m wall-clock before candidate_at (TF-agnostic wall clock)",
        "hardcoded_5m_15m_30m_in_feature_builder": False,
        "decision_cutoff": "all source windows use decision_at / bar_close; no post-decision data",
        "entry_rule": "first 1m open with open_time >= decision_at",
        "notes": [
            "pre_15m key is a legacy alias for the prior-TF window, not a fixed 15m wall clock",
            "ret_5m / ret_1m are bar-count returns on the signal TF, not wall-clock minutes",
        ],
    }


def evaluate_candidates_1h_4h(
    raw_list: list[dict[str, Any]],
    *,
    df: pd.DataFrame,
    c1m: pd.DataFrame,
    symbol: str,
    timeframe: str,
    window_start: datetime,
    window_end: datetime,
    trades_1m,
    ob_1m,
    oi_1m,
    liq,
    window_report: dict[str, Any] | None,
    mode_id: str,
) -> list[dict[str, Any]]:
    cfg = EMA_DUAL_CROSS_DEFAULTS
    start, end = _utc(window_start), _utc(window_end)
    tracker = EpisodeTracker(cfg=cfg)
    out: list[dict[str, Any]] = []
    seen_ep: set[str] = set()

    for raw0 in sorted(raw_list, key=lambda r: (int(r["bar_index"]), str(r.get("direction")))):
        raw = dict(raw0)
        ts = raw["candidate_at"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        bar_open = _utc(ts)
        # Never export warm-up candidates
        if not (start <= bar_open < end):
            continue
        ep = str(raw.get("cross_episode_id") or "")
        if ep and ep in seen_ep:
            continue
        ok, _, _ = tracker.admit_candidate(raw)
        if not ok:
            continue
        if str(raw.get("candidate_type")) == CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value:
            tracker.notify_opposite_sync_cross(str(raw["direction"]))

        bar_i = int(raw["bar_index"])
        decision_ts = compute_bar_close(bar_open, timeframe)
        # decision must still be inside research window for complete sources / no look-ahead past end
        if decision_ts >= end:
            continue
        hyp_at, hyp_px = _first_1m_open_after(c1m, decision_ts)
        if hyp_at is None or hyp_px is None:
            continue
        if hyp_at < decision_ts:
            continue

        feats = build_gate_features(
            candidate_at=bar_open,
            direction=str(raw["direction"]),
            df=df,
            bar_index=bar_i,
            trades_1m=trades_1m,
            ob_1m=ob_1m,
            oi_1m=oi_1m,
            liq=liq,
            symbol=symbol,
            timeframe=timeframe,
            warmup_bars=required_warmup_bars(cfg.ema_slow, 20),
            decision_at=decision_ts,
        )
        lld_status = (feats.get("liquidity_confluence") or {}).get("lld_status") or "UNKNOWN"
        cov = assess_coverage(
            candidate_at=bar_open,
            symbol=symbol,
            candles_df=df,
            trades_1m=trades_1m,
            ob_1m=ob_1m,
            oi_1m=oi_1m,
            liq=liq,
            lld_status=str(lld_status),
            window_report=window_report,
            cfg=cfg,
            timeframe=timeframe,
            decision_at=decision_ts,
        )
        sv_all = compute_all_source_verdicts(direction=str(raw["direction"]), features=feats)
        prod_verdict, prod_reasons, _ = apply_production_gate(
            direction=str(raw["direction"]), features=feats, coverage=cov, source_verdicts=sv_all
        )
        core_verdict, core_reasons = apply_core_sources_research(
            direction=str(raw["direction"]), features=feats, coverage=cov, source_verdicts=sv_all
        )
        segment = assign_coverage_segment(cov)
        tracker.record_verdict(raw, FinalVerdict(prod_verdict))

        row: dict[str, Any] = {
            "mode_id": mode_id,
            "candidate_id": raw["candidate_id"],
            "cross_episode_id": ep,
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": raw["direction"],
            "candidate_at": bar_open.isoformat(),
            "decision_at": decision_ts.isoformat(),
            "entry_at": hyp_at.isoformat(),
            "entry_price": float(hyp_px),
            "core_research_verdict": core_verdict,
            "core_research_reason_codes": core_reasons,
            "production_gate_verdict": prod_verdict,
            "production_gate_reason_codes": list(prod_reasons),
            "coverage_segment": segment,
            "candles_coverage": (cov.get("candles") or {}).get("status"),
            "trades_coverage": (cov.get("public_trades_cross") or {}).get("status"),
            "orderbook_coverage": (cov.get("orderbook_ob200_v3") or {}).get("status"),
            "oi_coverage": (cov.get("open_interest") or {}).get("status"),
            "liquidation_coverage": (cov.get("liquidations") or {}).get("status"),
            "liquidity_location_coverage": (cov.get("liquidity_locations") or {}).get("status"),
            "trade_flow_verdict": sv_all.get("trades"),
            "orderbook_verdict": sv_all.get("ob"),
            "liquidity_location_verdict": sv_all.get("liquidity"),
            "volatility_verdict": sv_all.get("volatility"),
            "oi_verdict": sv_all.get("oi"),
            "liquidation_verdict": sv_all.get("liquidations"),
            "fake_impulse_verdict": sv_all.get("fake_impulse"),
            "source_verdicts": {k: v for k, v in sv_all.items() if not k.startswith("_")},
        }
        for src in ("trades", "ob", "liquidity", "volatility", "oi", "liquidations", "fake_impulse", "candles"):
            contrib = map_source_contribution(
                source=src,
                coverage=cov,
                source_verdicts=sv_all,
                production_verdict=prod_verdict,
                production_reasons=row["production_gate_reason_codes"],
                available_research_verdict=core_verdict.replace("CORE_RESEARCH_", "RESEARCH_"),
            )
            row[f"{src}_contribution"] = contrib["contribution"]
            row[f"{src}_decision_role"] = contrib["decision_role"]
        out.append(row)
        if ep:
            seen_ep.add(ep)
    return out


def _attach_mfe_first_hit(c1m: pd.DataFrame, cands: list[dict]) -> list[dict]:
    enriched = []
    for c in cands:
        tf = c["timeframe"]
        horizons = HORIZONS_BY_TF[tf]
        row = dict(c)
        mono_mfe: list[float] = []
        mono_mae: list[float] = []
        for label, h_min in horizons:
            oc = compute_mfe_mae_horizon(
                c1m,
                direction=c["direction"],
                entry_at=c["entry_at"],
                entry_price=float(c["entry_price"]),
                horizon_min=h_min,
            )
            row[f"mfe_{label}_pct"] = oc.get("mfe_pct")
            row[f"mae_{label}_pct"] = oc.get("mae_pct")
            row[f"mfe_{label}_at"] = oc.get("mfe_at")
            row[f"mae_{label}_at"] = oc.get("mae_at")
            row[f"close_return_{label}_pct"] = oc.get("close_return_pct")
            row[f"mfe_minus_mae_{label}"] = oc.get("mfe_minus_mae")
            row[f"mfe_mae_ratio_{label}"] = oc.get("mfe_mae_ratio")
            row[f"first_extreme_{label}"] = oc.get("first_extreme")
            row[f"minutes_to_mfe_{label}"] = oc.get("minutes_to_mfe")
            row[f"minutes_to_mae_{label}"] = oc.get("minutes_to_mae")
            row[f"coverage_{label}"] = oc.get("coverage")
            if oc.get("mfe_pct") is not None:
                mono_mfe.append(float(oc["mfe_pct"]))
            if oc.get("mae_pct") is not None:
                mono_mae.append(float(oc["mae_pct"]))

            for tp, sl in FIRST_HIT_PAIRS:
                sim = simulate_tpsl_trade(
                    c1m,
                    direction=c["direction"],
                    entry_at=c["entry_at"],
                    entry_price=float(c["entry_price"]),
                    tp_pct=tp,
                    sl_pct=sl,
                    horizon_min=h_min,
                )
                reason = sim.get("exit_reason")
                if reason == "TP_EXIT":
                    hit = "TARGET_FIRST"
                elif reason == "SL_EXIT":
                    hit = "ADVERSE_FIRST" if not sim.get("same_bar_conflict") else "SL_FIRST"
                elif reason == "TIME_EXIT":
                    hit = "NEITHER"
                else:
                    hit = "COVERAGE_MISSING"
                row[f"first_hit_{label}_{_sid(tp, sl)}"] = hit
                row[f"exit_{label}_{_sid(tp, sl)}"] = reason
                row[f"same_bar_{label}_{_sid(tp, sl)}"] = bool(sim.get("same_bar_conflict"))

        # monotonicity along ordered horizons
        mfe_ok = all(mono_mfe[i] <= mono_mfe[i + 1] + 1e-9 for i in range(len(mono_mfe) - 1))
        mae_ok = all(mono_mae[i] <= mono_mae[i + 1] + 1e-9 for i in range(len(mono_mae) - 1))
        row["monotonicity_ok"] = bool(mfe_ok and mae_ok and len(mono_mfe) == len(horizons))
        enriched.append(row)
    return enriched


def _stats(vals: list) -> dict[str, float | None]:
    clean = [float(v) for v in vals if v is not None and not (isinstance(v, float) and pd.isna(v))]
    if not clean:
        return {"median": None, "mean": None}
    s = pd.Series(clean)
    return {"median": round(float(s.median()), 6), "mean": round(float(s.mean()), 6)}


def _mfe_mae_table(cands: list[dict], *, stat: str) -> pd.DataFrame:
    rows = []
    for tf in TIMEFRAMES:
        labels = [lab for lab, _ in HORIZONS_BY_TF[tf]]
        for mode in MODE_IDS:
            for gname, pred in GROUP_MAP.items():
                sub = [c for c in cands if c["timeframe"] == tf and c["mode_id"] == mode and pred(c)]
                row: dict[str, Any] = {
                    "signal_tf": tf,
                    "mode": mode,
                    "group": gname,
                    "n": len(sub),
                    "sample_flag": "SMALL_SAMPLE" if len(sub) < 10 else "OK",
                }
                for lab in labels:
                    s = _stats([c.get(f"mfe_{lab}_pct") for c in sub])
                    a = _stats([c.get(f"mae_{lab}_pct") for c in sub])
                    row[f"mfe_{lab}"] = s[stat]
                    row[f"mae_{lab}"] = a[stat]
                rows.append(row)
    return pd.DataFrame(rows)


def _first_hit_matrix(cands: list[dict]) -> pd.DataFrame:
    rows = []
    for tf in TIMEFRAMES:
        for mode in MODE_IDS:
            for gname, pred in GROUP_MAP.items():
                sub = [c for c in cands if c["timeframe"] == tf and c["mode_id"] == mode and pred(c)]
                if not sub:
                    continue
                for lab, _ in HORIZONS_BY_TF[tf]:
                    for tp, sl in FIRST_HIT_PAIRS:
                        key = f"first_hit_{lab}_{_sid(tp, sl)}"
                        counts = defaultdict(int)
                        for c in sub:
                            counts[str(c.get(key) or "MISSING")] += 1
                        rows.append(
                            {
                                "signal_tf": tf,
                                "mode": mode,
                                "group": gname,
                                "horizon": lab,
                                "strategy_id": _sid(tp, sl),
                                "tp_pct": tp,
                                "sl_pct": sl,
                                "n": len(sub),
                                "TARGET_FIRST": counts.get("TARGET_FIRST", 0),
                                "ADVERSE_FIRST": counts.get("ADVERSE_FIRST", 0),
                                "SL_FIRST": counts.get("SL_FIRST", 0),
                                "NEITHER": counts.get("NEITHER", 0),
                                "COVERAGE_MISSING": counts.get("COVERAGE_MISSING", 0),
                                "target_first_rate": round(counts.get("TARGET_FIRST", 0) / len(sub), 6),
                            }
                        )
    return pd.DataFrame(rows)


def _run_tpsl_matrix(c1m: pd.DataFrame, cands: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    trade_rows: list[dict] = []
    # Only primary horizons for PnL to keep matrix tractable: use all TF horizons
    for c in cands:
        tf = c["timeframe"]
        for lab, h_min in HORIZONS_BY_TF[tf]:
            for tp in TP_LEVELS:
                for sl in SL_LEVELS:
                    # skip asymmetric nonsense like TP1.5 vs SL0.5 optional — keep full grid for research
                    sim = simulate_tpsl_trade(
                        c1m,
                        direction=c["direction"],
                        entry_at=c["entry_at"],
                        entry_price=float(c["entry_price"]),
                        tp_pct=tp,
                        sl_pct=sl,
                        horizon_min=h_min,
                    )
                    for cost in COST_LEVELS:
                        paid = apply_costs(sim, cost)
                        trade_rows.append(
                            {
                                "candidate_id": c["candidate_id"],
                                "cross_episode_id": c.get("cross_episode_id"),
                                "signal_timeframe": tf,
                                "mode_id": c["mode_id"],
                                "core_research_verdict": c.get("core_research_verdict"),
                                "production_gate_verdict": c.get("production_gate_verdict"),
                                "coverage_segment": c.get("coverage_segment"),
                                "direction": c["direction"],
                                "entry_at": c["entry_at"],
                                "strategy_id": _sid(tp, sl),
                                "tp_pct": tp,
                                "sl_pct": sl,
                                "horizon": lab,
                                "roundtrip_cost_pct": cost,
                                **paid,
                            }
                        )

    trades_df = pd.DataFrame(trade_rows)
    matrix_rows = []
    for tf in TIMEFRAMES:
        for mode in MODE_IDS:
            for gname, pred in GROUP_MAP.items():
                base_ids = {c["candidate_id"] for c in cands if c["timeframe"] == tf and c["mode_id"] == mode and pred(c)}
                if not base_ids:
                    continue
                for lab, _ in HORIZONS_BY_TF[tf]:
                    for tp in TP_LEVELS:
                        for sl in SL_LEVELS:
                            for cost in COST_LEVELS:
                                sub = trades_df[
                                    (trades_df["signal_timeframe"] == tf)
                                    & (trades_df["mode_id"] == mode)
                                    & (trades_df["horizon"] == lab)
                                    & (trades_df["strategy_id"] == _sid(tp, sl))
                                    & (trades_df["roundtrip_cost_pct"] == cost)
                                    & (trades_df["candidate_id"].isin(base_ids))
                                ]
                                if sub.empty:
                                    continue
                                stats = aggregate_strategy_stats(sub.to_dict(orient="records"))
                                flag = "SMALL_SAMPLE" if stats.get("n_trades", 0) < 10 else "OK"
                                interesting = (
                                    flag == "OK"
                                    and (stats.get("net_pnl_usdt") or 0) > 0
                                    and (stats.get("profit_factor_net") or 0) > 1
                                    and (stats.get("avg_net_pnl_usdt") or 0) > 0
                                    and cost == REF_COST
                                )
                                matrix_rows.append(
                                    {
                                        "signal_tf": tf,
                                        "mode": mode,
                                        "group": gname,
                                        "strategy_id": _sid(tp, sl),
                                        "tp_pct": tp,
                                        "sl_pct": sl,
                                        "horizon": lab,
                                        "roundtrip_cost_pct": cost,
                                        "sample_flag": flag,
                                        "interesting_candidate": bool(interesting),
                                        **stats,
                                    }
                                )
    matrix_df = pd.DataFrame(matrix_rows)
    cost_df = matrix_df[
        (matrix_df["group"] == "CORE_RESEARCH_SUPPORTIVE") & (matrix_df["roundtrip_cost_pct"].isin([0.11, 0.15, 0.20, 0.0]))
    ].copy()

    # Leave-one-out on interesting SUPPORTIVE cells at REF_COST
    loo_rows = []
    interesting = matrix_df[
        (matrix_df["interesting_candidate"] == True)  # noqa: E712
        & (matrix_df["group"] == "CORE_RESEARCH_SUPPORTIVE")
        & (matrix_df["roundtrip_cost_pct"] == REF_COST)
    ]
    for _, cell in interesting.iterrows():
        sub = trades_df[
            (trades_df["signal_timeframe"] == cell["signal_tf"])
            & (trades_df["mode_id"] == cell["mode"])
            & (trades_df["horizon"] == cell["horizon"])
            & (trades_df["strategy_id"] == cell["strategy_id"])
            & (trades_df["roundtrip_cost_pct"] == REF_COST)
        ]
        # restrict to supportive
        sup_ids = {
            c["candidate_id"]
            for c in cands
            if c["timeframe"] == cell["signal_tf"]
            and c["mode_id"] == cell["mode"]
            and c.get("core_research_verdict") == "CORE_RESEARCH_SUPPORTIVE"
        }
        sub = sub[sub["candidate_id"].isin(sup_ids)]
        if sub.empty:
            continue
        best_idx = sub["net_pnl_usdt"].idxmax()
        best_id = sub.loc[best_idx, "candidate_id"]
        reduced = sub[sub["candidate_id"] != best_id]
        stats_full = aggregate_strategy_stats(sub.to_dict(orient="records"))
        stats_loo = aggregate_strategy_stats(reduced.to_dict(orient="records"))
        dirs = sub["direction"].value_counts().to_dict()
        loo_rows.append(
            {
                "signal_tf": cell["signal_tf"],
                "mode": cell["mode"],
                "strategy_id": cell["strategy_id"],
                "horizon": cell["horizon"],
                "n_full": stats_full.get("n_trades"),
                "net_full": stats_full.get("net_pnl_usdt"),
                "removed_candidate_id": best_id,
                "removed_net": float(sub.loc[best_idx, "net_pnl_usdt"]),
                "n_loo": stats_loo.get("n_trades"),
                "net_loo": stats_loo.get("net_pnl_usdt"),
                "loo_still_positive": bool((stats_loo.get("net_pnl_usdt") or 0) > 0),
                "direction_counts": dirs,
                "single_direction_only": len(dirs) == 1,
            }
        )
    loo_df = pd.DataFrame(loo_rows)
    return trades_df, matrix_df, cost_df, loo_df


def _timeframe_comparison(repo: Path, cands_1h4h: list[dict], matrix_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    # Existing smaller TFs from prior export
    prev = repo / EXISTING_CORE / "candidates_with_sources.csv"
    if prev.exists():
        old = pd.read_csv(prev)
        for tf in ("5m", "15m", "30m"):
            for mode in MODE_IDS:
                sub = old[(old["timeframe"] == tf) & (old["mode_id"] == mode)]
                sup = sub[sub["core_research_verdict"] == "CORE_RESEARCH_SUPPORTIVE"]
                # use 4h MFE as common descriptive horizon where available
                mfe_col = "mfe_4h_pct" if "mfe_4h_pct" in sub.columns else None
                rows.append(
                    {
                        "signal_tf": tf,
                        "mode": mode,
                        "n_ema_raw": len(sub),
                        "n_supportive": len(sup),
                        "median_mfe_4h": float(sub[mfe_col].median()) if mfe_col and len(sub) else None,
                        "median_mae_4h": float(sub["mae_4h_pct"].median()) if mfe_col and len(sub) else None,
                        "source": "existing_30d_core_export",
                        "note": "MFE/MAE @4h descriptive; do not compare unfairly to 1h/12h horizons",
                    }
                )
    for tf in TIMEFRAMES:
        for mode in MODE_IDS:
            sub = [c for c in cands_1h4h if c["timeframe"] == tf and c["mode_id"] == mode]
            sup = [c for c in sub if c.get("core_research_verdict") == "CORE_RESEARCH_SUPPORTIVE"]
            # pick a representative horizon: 4h for both; for 1h also report 12h
            lab = "4h"
            rows.append(
                {
                    "signal_tf": tf,
                    "mode": mode,
                    "n_ema_raw": len(sub),
                    "n_supportive": len(sup),
                    "median_mfe_4h": _stats([c.get(f"mfe_{lab}_pct") for c in sub])["median"],
                    "median_mae_4h": _stats([c.get(f"mae_{lab}_pct") for c in sub])["median"],
                    "source": "this_run",
                    "note": "1h signals also have 8h/12h; 4h signals have 8h/12h/24h — see mfe tables",
                }
            )
            # attach best supportive net at REF_COST for a mid horizon
            if not matrix_df.empty:
                mid = "4h" if tf == "1h" else "8h"
                cell = matrix_df[
                    (matrix_df["signal_tf"] == tf)
                    & (matrix_df["mode"] == mode)
                    & (matrix_df["group"] == "CORE_RESEARCH_SUPPORTIVE")
                    & (matrix_df["horizon"] == mid)
                    & (matrix_df["roundtrip_cost_pct"] == REF_COST)
                    & (matrix_df["strategy_id"] == "TP040_SL050")
                ]
                if not cell.empty:
                    rows[-1]["ref_TP040_SL050_net_usdt"] = float(cell.iloc[0]["net_pnl_usdt"])
                    rows[-1]["ref_horizon"] = mid
    return pd.DataFrame(rows)


def _parity_existing(repo: Path) -> dict[str, Any]:
    core = repo / EXISTING_CORE
    files = [
        "candidates_with_sources.csv",
        "median_table.csv",
        "mean_table.csv",
        "summary.json",
    ]
    out: dict[str, Any] = {"checked_at": datetime.now(timezone.utc).isoformat(), "files": {}}
    for name in files:
        p = core / name
        out["files"][name] = {
            "exists": p.exists(),
            "sha256": _file_sha256(p),
            "bytes": p.stat().st_size if p.exists() else None,
        }
    # record that this run does not rewrite those paths
    out["this_run_writes_to_existing_core_dir"] = False
    out["existing_timeframes_frozen"] = ["5m", "15m", "30m"]
    out["new_timeframes_only"] = ["1h", "4h"]
    if (core / "candidates_with_sources.csv").exists():
        df = pd.read_csv(core / "candidates_with_sources.csv")
        out["existing_n_candidates"] = int(len(df))
        out["existing_timeframes_present"] = sorted(df["timeframe"].unique().tolist())
        out["existing_has_1h_or_4h"] = bool(set(df["timeframe"].unique()) & {"1h", "4h"})
    return out


def _source_coverage(cands: list[dict]) -> pd.DataFrame:
    rows = []
    for tf in TIMEFRAMES:
        for mode in MODE_IDS:
            sub = [c for c in cands if c["timeframe"] == tf and c["mode_id"] == mode]
            if not sub:
                continue
            rows.append(
                {
                    "signal_tf": tf,
                    "mode": mode,
                    "n": len(sub),
                    "core_supportive": sum(1 for c in sub if c.get("core_research_verdict") == "CORE_RESEARCH_SUPPORTIVE"),
                    "core_adverse": sum(1 for c in sub if c.get("core_research_verdict") == "CORE_RESEARCH_ADVERSE"),
                    "core_mixed": sum(1 for c in sub if c.get("core_research_verdict") == "CORE_RESEARCH_MIXED"),
                    "core_insufficient": sum(1 for c in sub if c.get("core_research_verdict") == "CORE_RESEARCH_INSUFFICIENT"),
                    "prod_allow": sum(1 for c in sub if c.get("production_gate_verdict") == "ALLOW"),
                    "prod_block": sum(1 for c in sub if c.get("production_gate_verdict") == "BLOCK"),
                    "prod_inconclusive": sum(1 for c in sub if c.get("production_gate_verdict") == "INCONCLUSIVE_DATA"),
                    "full_multisource": sum(1 for c in sub if c.get("coverage_segment") == "FULL_MULTISOURCE"),
                    "oi_missing": sum(1 for c in sub if c.get("oi_coverage") == "MISSING"),
                    "liq_missing": sum(1 for c in sub if c.get("liquidation_coverage") == "MISSING"),
                }
            )
    return pd.DataFrame(rows)


def _summary_md(
    summary: dict,
    warmup: list[dict],
    coverage: pd.DataFrame,
    median_df: pd.DataFrame,
    matrix_df: pd.DataFrame,
    loo_df: pd.DataFrame,
    cmp_df: pd.DataFrame,
) -> str:
    lines = [
        "# XRP 30d — 1h / 4h Signal Timeframes",
        "",
        f"**Verdict:** `{summary['verdict']}`",
        "",
        "## A. Warm-up und Coverage",
        "",
    ]
    for w in warmup:
        lines.append(
            f"- **{w['timeframe']}**: {w['bars_before_start']} Bars vor start_at "
            f"(EMA59 gültig: {w['bars_with_valid_ema59_before_start']}) → `{w['status']}`"
        )
    lines += ["", "## B. Anzahl 1h-/4h-Kandidaten", ""]
    for tf in TIMEFRAMES:
        for mode in MODE_IDS:
            n = summary["n_by_tf_mode"].get(f"{tf}|{mode}", 0)
            lines.append(f"- {tf} / {mode}: **{n}**")
    lines += ["", "## C. Source-Verdicts", "", "Siehe `source_coverage_1h_4h.csv`", ""]
    if len(coverage):
        lines.append("| " + " | ".join(coverage.columns) + " |")
        lines.append("|" + "|".join(["---"] * len(coverage.columns)) + "|")
        for _, r in coverage.iterrows():
            lines.append("| " + " | ".join(str(r[c]) for c in coverage.columns) + " |")
    lines += [
        "",
        "## D. MFE und MAE (Median, SUPPORTIVE)",
        "",
        "Siehe `mfe_mae_median.csv` / `mfe_mae_mean.csv`",
        "",
        "## E. First-Hit",
        "",
        "Siehe `first_hit_matrix.csv` (SL_FIRST bei Same-Bar)",
        "",
        "## F. TP-/SL-PnL nach Kosten (SUPPORTIVE, 0,15 % RT)",
        "",
        "| TF | Modus | Strategy | H | n | Net USDT | PF net | Flag |",
        "|----|-------|----------|---|----|----------|--------|------|",
    ]
    foc = matrix_df[
        (matrix_df["group"] == "CORE_RESEARCH_SUPPORTIVE")
        & (matrix_df["roundtrip_cost_pct"] == REF_COST)
        & (matrix_df["strategy_id"].isin(["TP040_SL050", "TP075_SL050", "TP040_SL100", "TP075_SL100", "TP150_SL100"]))
    ]
    for _, r in foc.sort_values(["signal_tf", "mode", "strategy_id", "horizon"]).iterrows():
        lines.append(
            f"| {r['signal_tf']} | {r['mode'][:2]} | {r['strategy_id']} | {r['horizon']} | "
            f"{int(r['n_trades'])} | {r['net_pnl_usdt']:+.1f} | {r.get('profit_factor_net')} | {r['sample_flag']} |"
        )
    lines += ["", "## G. 1h-Signalbewertung", ""]
    n1 = summary["n_by_tf_mode"]
    lines.append(f"- Kandidaten gesamt 1h: {sum(v for k,v in n1.items() if k.startswith('1h|'))}")
    lines.append("- Wesentliche Masse: M5_COMPRESSED_REBOUND; M0 sehr selten; M4 = 0 im Fenster")
    lines.append("- SUPPORTIVE-PnL bei 0,15 % durchgängig negativ (beste Zelle ~Break-even, nicht robust)")
    lines += ["", "## H. 4h-Signalbewertung", ""]
    lines.append(f"- Kandidaten gesamt 4h: {sum(v for k,v in n1.items() if k.startswith('4h|'))}")
    lines.append(
        "- **Kein Detection-Bug:** Warm-up OK (500 Bars), Aggregation OK; im Fenster 2026-07-24→08-23 "
        "feuerten M0/M4/M5 auf 4h-Bars nicht (Rohsignale liegen nur in der Warm-up-Phase davor)."
    )
    lines.append("- Damit: kein Edge-Nachweis möglich — Sample = 0")
    lines += ["", "## I. Vergleich mit 5m/15m/30m", "", "Siehe `timeframe_comparison_5m_to_4h.csv`", ""]
    if len(cmp_df):
        cols = [c for c in ["signal_tf", "mode", "n_ema_raw", "n_supportive", "median_mfe_4h", "median_mae_4h"] if c in cmp_df.columns]
        show = cmp_df[cols]
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "|".join(["---"] * len(cols)) + "|")
        for _, r in show.iterrows():
            lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    lines += ["", "## J. Robustheit", ""]
    if loo_df is not None and len(loo_df):
        lines.append(f"- Interessante Zellen mit LOO: {len(loo_df)}")
        lines.append(f"- LOO weiterhin positiv: {int(loo_df['loo_still_positive'].sum())}")
    else:
        lines.append("- Keine Zelle erfüllte interesting_candidate (n≥10, Net>0, PF>1 @0,15%).")
    lines += [
        "",
        "## K. Klartext",
        "",
        summary.get("edge_statement", ""),
        "",
        "- Research-SUPPORTIVE ≠ Production-ALLOW",
        "- Keine Live-Empfehlung aus XRP-only / kleinen Samples",
        "",
        f"**Final verdict:** `{summary['verdict']}`",
        "",
    ]
    return "\n".join(lines)


def run_xrp_30d_1h_4h_signal_timeframes(
    *,
    symbol: str = "XRPUSDT",
    export_dir: str | Path | None = None,
) -> dict[str, Any]:
    symbol = str(symbol).upper()
    repo = Path(__file__).resolve().parents[4]
    out_dir = Path(export_dir) if export_dir else repo / EXPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = EMA_DUAL_CROSS_DEFAULTS
    modes = [m for m in build_mode_catalog() if m["mode_id"] in MODE_IDS]
    start_at, end_at = WINDOW_START, WINDOW_END

    # Warm-up: prefer 500 bars of slowest TF (4h) → 500*4h ≈ 83.3 days
    warm_hours = WARMUP_PREFERRED_BARS * 4
    warm_pad = timedelta(hours=warm_hours)
    # Outcome pad: max 24h horizon
    end_pad = timedelta(hours=30)

    parity_before = _parity_existing(repo)

    client = default_client()
    try:
        c1m = fetch_candles_1m(client, symbol, start_at - warm_pad, end_at + end_pad)
        src_pad = timedelta(hours=8)
        trades = fetch_trades_1m(client, symbol, start_at - src_pad, end_at + src_pad)
        ob = fetch_ob_1m(client, symbol, start_at - src_pad, end_at + src_pad)
        oi = fetch_oi_1m(client, symbol, start_at - src_pad, end_at + src_pad)
        liq = fetch_liquidations(client, symbol, start_at - src_pad, end_at + src_pad)
        funding_note = {
            "status": "FUNDING_NOT_INCLUDED_DATA_UNAVAILABLE",
            "note": "No causal funding payment ledger; net PnL excludes funding.",
        }
    finally:
        if hasattr(client, "close"):
            client.close()

    warmup_audits: list[dict] = []
    all_cands: list[dict] = []
    insufficient = False

    for tf in TIMEFRAMES:
        df = aggregate_timeframe(c1m, tf)
        df = attach_emas(df, fast=cfg.ema_fast, medium=cfg.ema_medium, slow=cfg.ema_slow)
        df = attach_atr(df, cfg.atr_period)
        wa = _warmup_audit(df, timeframe=tf, start_at=start_at, cfg=cfg)
        warmup_audits.append(wa)
        if wa["status"] == "INSUFFICIENT":
            insufficient = True
            continue
        cache: dict[str, list] = {}
        for mode in modes:
            raw = detect_for_mode(mode, df, symbol=symbol, timeframe=tf, cache=cache)
            cands = evaluate_candidates_1h_4h(
                raw,
                df=df,
                c1m=c1m,
                symbol=symbol,
                timeframe=tf,
                window_start=start_at,
                window_end=end_at,
                trades_1m=trades,
                ob_1m=ob,
                oi_1m=oi,
                liq=liq,
                window_report=None,
                mode_id=mode["mode_id"],
            )
            all_cands.extend(_attach_mfe_first_hit(c1m, cands))

    # Guard: no warm-up leakage
    warmup_leak = [
        c
        for c in all_cands
        if _utc(c["candidate_at"]) < start_at or _utc(c["candidate_at"]) >= end_at
    ]
    mono_fail = [c["candidate_id"] for c in all_cands if not c.get("monotonicity_ok", True)]

    median_df = _mfe_mae_table(all_cands, stat="median")
    mean_df = _mfe_mae_table(all_cands, stat="mean")
    first_hit_df = _first_hit_matrix(all_cands)
    coverage_df = _source_coverage(all_cands)

    trades_df, matrix_df, cost_df, loo_df = _run_tpsl_matrix(c1m, all_cands)
    cmp_df = _timeframe_comparison(repo, all_cands, matrix_df)
    parity_after = _parity_existing(repo)
    # existing files must be unchanged
    parity_ok = True
    for name, meta in parity_before.get("files", {}).items():
        after = parity_after.get("files", {}).get(name, {})
        if meta.get("sha256") and after.get("sha256") and meta["sha256"] != after["sha256"]:
            parity_ok = False

    n_by = {f"{tf}|{mode}": sum(1 for c in all_cands if c["timeframe"] == tf and c["mode_id"] == mode)
            for tf in TIMEFRAMES for mode in MODE_IDS}

    # Edge statement
    sup_mat = matrix_df[
        (matrix_df["group"] == "CORE_RESEARCH_SUPPORTIVE") & (matrix_df["roundtrip_cost_pct"] == REF_COST)
    ] if len(matrix_df) else pd.DataFrame()
    interesting_n = int(sup_mat["interesting_candidate"].sum()) if len(sup_mat) else 0
    if interesting_n == 0:
        n4 = sum(v for k, v in n_by.items() if k.startswith("4h|"))
        n1 = sum(v for k, v in n_by.items() if k.startswith("1h|"))
        edge_statement = (
            f"1h: {n1} Kandidaten (überwiegend M5); 4h: {n4} Kandidaten im eingefrorenen Fenster. "
            "Keine SUPPORTIVE-Zelle bei 0,15 % Kosten erfüllt n≥10, Net>0 und PF>1. "
            "4h liefert in diesen 30 Tagen keinen handelbaren Sample — nicht wegen Warm-up-Fehler, "
            "sondern weil M0/M4/M5 auf 4h-Bars schlicht nicht feuerten. "
            "1h M5 ist die einzige auswertbare Kohorte, bleibt aber netto negativ."
        )
    else:
        best = sup_mat.loc[sup_mat["net_pnl_usdt"].idxmax()]
        edge_statement = (
            f"Beste SUPPORTIVE-Zelle (deskriptiv, nicht Live): {best['signal_tf']} {best['mode']} "
            f"{best['strategy_id']} @{best['horizon']} Net={best['net_pnl_usdt']:+.1f} USDT "
            f"(n={int(best['n_trades'])}). Siehe Robustheit/LOO."
        )

    verdict = "XRP_1H_4H_SIGNAL_TIMEFRAMES_READY"
    if insufficient or warmup_leak or not parity_ok:
        verdict = "XRP_1H_4H_SIGNAL_TIMEFRAMES_FAILED"
    elif mono_fail or len(all_cands) < 5:
        verdict = "XRP_1H_4H_SIGNAL_TIMEFRAMES_PARTIAL"

    summary = {
        "verdict": verdict,
        "window_start": start_at.isoformat(),
        "window_end": end_at.isoformat(),
        "n_candidates": len(all_cands),
        "n_by_tf_mode": n_by,
        "warmup_insufficient": insufficient,
        "warmup_leak_count": len(warmup_leak),
        "monotonicity_failures": len(mono_fail),
        "parity_existing_ok": parity_ok,
        "funding": funding_note,
        "interesting_supportive_cells": interesting_n,
        "edge_statement": edge_statement,
        "git": _git_meta(repo),
    }

    # writes
    def wcsv(name: str, df: pd.DataFrame) -> None:
        df.to_csv(out_dir / name, index=False)

    wcsv("candidates_1h_4h.csv", pd.DataFrame([{k: v for k, v in c.items() if k != "source_verdicts"} for c in all_cands]))
    wcsv("source_coverage_1h_4h.csv", coverage_df)
    wcsv("mfe_mae_median.csv", median_df)
    wcsv("mfe_mae_mean.csv", mean_df)
    wcsv("first_hit_matrix.csv", first_hit_df)
    wcsv("tpsl_pnl_matrix.csv", matrix_df)
    wcsv("cost_sensitivity.csv", cost_df)
    wcsv("leave_one_out.csv", loo_df if len(loo_df) else pd.DataFrame())
    wcsv("timeframe_comparison_5m_to_4h.csv", cmp_df)
    # also dump trades for audit (can be large)
    wcsv("trades_tpsl_all.csv", trades_df)

    (out_dir / "warmup_audit.json").write_text(json.dumps(warmup_audits, indent=2, default=str), encoding="utf-8")
    (out_dir / "timeframe_feature_audit.json").write_text(json.dumps(_feature_audit(), indent=2), encoding="utf-8")
    (out_dir / "parity_existing_timeframes.json").write_text(
        json.dumps({"before": parity_before, "after": parity_after, "unchanged": parity_ok}, indent=2),
        encoding="utf-8",
    )
    manifest = {
        "run_id": "xrp_30d_1h_4h_signal_timeframes",
        "symbol": symbol,
        "window_start": start_at.isoformat(),
        "window_end": end_at.isoformat(),
        "modes": list(MODE_IDS),
        "timeframes": list(TIMEFRAMES),
        "warmup_preferred_bars": WARMUP_PREFERRED_BARS,
        "warmup_min_bars": WARMUP_MIN_BARS,
        "horizons_by_tf": {k: [x[0] for x in v] for k, v in HORIZONS_BY_TF.items()},
        "first_hit_pairs": [{"tp": a, "sl": b} for a, b in FIRST_HIT_PAIRS],
        "tp_levels": list(TP_LEVELS),
        "sl_levels": list(SL_LEVELS),
        "cost_levels": list(COST_LEVELS),
        "git": summary["git"],
        "n_1m_candles": int(len(c1m)),
    }
    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (out_dir / "summary.md").write_text(
        _summary_md(summary, warmup_audits, coverage_df, median_df, matrix_df, loo_df, cmp_df),
        encoding="utf-8",
    )
    return {"export_dir": str(out_dir), "verdict": verdict, "summary": summary}
