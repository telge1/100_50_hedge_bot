"""Canonical candidate evaluation (original XRP core_30d semantics)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from ....cluster_sweep_research.ema_features import required_warmup_bars
from ...config import EMA_DUAL_CROSS_DEFAULTS
from ...coverage_gate import assess_coverage
from ...episode_state import EpisodeTracker
from ...feature_builder import build_gate_features
from ...models import CandidateType, FinalVerdict
from ...timeframes import bar_close as compute_bar_close
from ..core_sources_research_policy import (
    apply_core_sources_research,
    apply_production_gate,
    assign_coverage_segment,
)
from ..mfe_runner import build_mode_catalog, detect_for_mode
from ..research_policy import compute_all_source_verdicts, map_source_contribution
from .entry import next_signal_tf_open
from .semantics import ENTRY_RULE, MULTICOIN_DETECTION_SCOPES


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def evaluate_candidates_canonical(
    raw_list: list[dict[str, Any]],
    *,
    df: pd.DataFrame,
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
    """Shared candidate builder: original XRP entry = next signal-TF open."""
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
        if not (start <= _utc(ts) < end):
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
        bar_open = _utc(ts)
        decision_ts = compute_bar_close(bar_open, timeframe)
        hyp_at, hyp_px = next_signal_tf_open(df, bar_i)
        if hyp_at is None or hyp_px is None:
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
        prod_verdict, prod_reasons, _prod_sv = apply_production_gate(
            direction=str(raw["direction"]), features=feats, coverage=cov, source_verdicts=sv_all
        )
        core_verdict, core_reasons = apply_core_sources_research(
            direction=str(raw["direction"]), features=feats, coverage=cov, source_verdicts=sv_all
        )
        # Hard rule: research labels must never be production ALLOW
        if core_verdict == "ALLOW" or str(core_verdict).endswith("_ALLOW"):
            raise RuntimeError("Research verdict leaked production ALLOW label")
        if prod_verdict == "CORE_RESEARCH_SUPPORTIVE":
            raise RuntimeError("Production gate leaked research SUPPORTIVE label")

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
            "entry_rule": ENTRY_RULE,
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


def modes_for_multicoin_scope() -> list[dict[str, Any]]:
    catalog = {m["mode_id"]: m for m in build_mode_catalog()}
    ids = sorted({m for _, m in MULTICOIN_DETECTION_SCOPES})
    return [catalog[m] for m in ids]


def detect_candidates_for_scopes(
    *,
    df_by_tf: dict[str, pd.DataFrame],
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    trades_1m,
    ob_1m,
    oi_1m,
    liq,
    window_report: dict[str, Any] | None,
    scopes: tuple[tuple[str, str], ...] = MULTICOIN_DETECTION_SCOPES,
) -> list[dict[str, Any]]:
    """Detect+evaluate candidates for explicit (timeframe, mode_id) scopes."""
    catalog = {m["mode_id"]: m for m in build_mode_catalog()}
    all_cands: list[dict[str, Any]] = []
    cache_by_tf: dict[str, dict[str, list]] = {}
    for tf, mode_id in scopes:
        df = df_by_tf[tf]
        cache = cache_by_tf.setdefault(tf, {})
        mode = catalog[mode_id]
        raw = detect_for_mode(mode, df, symbol=symbol, timeframe=tf, cache=cache)
        cands = evaluate_candidates_canonical(
            raw,
            df=df,
            symbol=symbol,
            timeframe=tf,
            window_start=window_start,
            window_end=window_end,
            trades_1m=trades_1m,
            ob_1m=ob_1m,
            oi_1m=oi_1m,
            liq=liq,
            window_report=window_report,
            mode_id=mode_id,
        )
        all_cands.extend(cands)
    return all_cands
