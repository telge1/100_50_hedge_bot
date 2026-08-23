"""Strict 1:1 XRP strategy parity audit (research-only; never overwrites SoT artifacts).

Writes exclusively under:
  results/edc_sync_tolerance/diagnostics/xrp_strategy_parity/
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..checkpoint import atomic_write_json
from ..coin_backtest import run_one_coin
from ..constants import (
    DEFAULT_END,
    DEFAULT_START,
    ENTRY_RULE,
    NOTIONAL_USDT,
    PRIMARY_CELLS,
    PRIMARY_COST_PCT,
    PRIMARY_GROUP,
    PRIMARY_MODE,
    PRIMARY_REFERENCE_CELL_ID,
    PRIMARY_TF,
)
from ..xrp_parity import DEFAULT_XRP_CANDIDATES_EXPORT, frozen_cells_match_xrp_matrix_defs

PRICE_TOL = 1e-10
PNL_TOL = 1e-10

ORIGINAL_CANDIDATES = DEFAULT_XRP_CANDIDATES_EXPORT
ORIGINAL_TRADES = "results/edc_sync_tolerance/xrp_30d_horizon_tp_sl_matrix/trades_matrix.csv"
ORIGINAL_MATRIX = "results/edc_sync_tolerance/xrp_30d_horizon_tp_sl_matrix/primary_supportive_matrix.csv"
ORIGINAL_MANIFEST = "results/edc_sync_tolerance/xrp_30d_core_sources_comparison/run_manifest.json"
ORIGINAL_MATRIX_SUMMARY = "results/edc_sync_tolerance/xrp_30d_horizon_tp_sl_matrix/summary.json"

AUDIT_ROOT_REL = "results/edc_sync_tolerance/diagnostics/xrp_strategy_parity"
REPLAY_REL = f"{AUDIT_ROOT_REL}/multicoin_xrp_replay_shared"


def _repo() -> Path:
    return Path(__file__).resolve().parents[6]


def _utc_iso(v: Any) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    except Exception:
        return str(v)


def _f(v: Any) -> float | None:
    if v is None or v == "" or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _close(a: Any, b: Any, tol: float = PNL_TOL) -> bool:
    fa, fb = _f(a), _f(b)
    if fa is None and fb is None:
        return True
    if fa is None or fb is None:
        return False
    return abs(fa - fb) <= tol


def identify_source_of_truth(repo: Path) -> dict[str, Any]:
    """Phase 1: identify original successful XRP run (no guessing)."""
    inventory = []
    pairs = [
        {
            "role": "CANDIDATE_SOT",
            "path": str(repo / ORIGINAL_CANDIDATES),
            "runner": "scripts/run_edc_xrp_30d_core_sources_comparison.py",
            "referenced_by_multicoin": True,
        },
        {
            "role": "PNL_SOT",
            "path": str(repo / ORIGINAL_TRADES),
            "runner": "scripts/run_edc_xrp_horizon_tp_sl_matrix.py",
            "referenced_by_multicoin": True,
        },
    ]
    for p in pairs:
        path = Path(p["path"])
        p["exists"] = path.exists()
        inventory.append(p)

    manifest = {}
    mp = repo / ORIGINAL_MANIFEST
    if mp.exists():
        manifest = json.loads(mp.read_text(encoding="utf-8"))

    matrix = pd.read_csv(repo / ORIGINAL_MATRIX) if (repo / ORIGINAL_MATRIX).exists() else pd.DataFrame()
    ref_row = None
    if not matrix.empty:
        hit = matrix[
            (matrix["signal_tf"] == "5m")
            & (matrix["mode"] == "M0_STRICT_SYNC")
            & (matrix["group"] == "CORE_RESEARCH_SUPPORTIVE")
            & (matrix["strategy_id"] == "TP075_SL050")
            & (matrix["horizon"] == "8h")
        ]
        if len(hit) == 1:
            ref_row = hit.iloc[0].to_dict()

    trades = pd.read_csv(repo / ORIGINAL_TRADES) if (repo / ORIGINAL_TRADES).exists() else pd.DataFrame()
    ref_trades = pd.DataFrame()
    if not trades.empty:
        ref_trades = trades[
            (trades["signal_timeframe"] == "5m")
            & (trades["mode_id"] == "M0_STRICT_SYNC")
            & (trades["group"] == "CORE_RESEARCH_SUPPORTIVE")
            & (trades["strategy_id"] == "TP075_SL050")
            & (trades["horizon"] == "8h")
        ].copy()

    cands = pd.read_csv(repo / ORIGINAL_CANDIDATES) if (repo / ORIGINAL_CANDIDATES).exists() else pd.DataFrame()
    ref_cands = pd.DataFrame()
    if not cands.empty:
        ref_cands = cands[
            (cands["timeframe"] == "5m")
            & (cands["mode_id"] == "M0_STRICT_SYNC")
            & (cands["core_research_verdict"] == "CORE_RESEARCH_SUPPORTIVE")
        ].copy()

    unambiguous = (
        all(x["exists"] for x in inventory)
        and ref_row is not None
        and len(ref_trades) == 15
        and abs(float(ref_row["net_pnl_usdt"]) - 27.5) < 1e-6
        and len(ref_cands) == 15
    )

    return {
        "status": "OK" if unambiguous else "XRP_SOURCE_OF_TRUTH_AMBIGUOUS",
        "inventory": inventory,
        "selected": {
            "candidates_export": str(repo / ORIGINAL_CANDIDATES),
            "trades_matrix": str(repo / ORIGINAL_TRADES),
            "primary_matrix": str(repo / ORIGINAL_MATRIX),
            "core_manifest": str(repo / ORIGINAL_MANIFEST),
            "window": {
                "start": manifest.get("start_at") or DEFAULT_START.isoformat(),
                "end": manifest.get("end_at") or DEFAULT_END.isoformat(),
            },
            "git": manifest.get("git"),
            "symbol": "XRPUSDT",
            "reference_cell": {
                "timeframe": "5m",
                "mode": "M0_STRICT_SYNC",
                "group": "CORE_RESEARCH_SUPPORTIVE",
                "strategy_id": "TP075_SL050",
                "tp_pct": 0.75,
                "sl_pct": 0.50,
                "horizon": "8h",
                "cost_pct": 0.15,
                "n_trades": int(len(ref_trades)),
                "net_pnl_usdt": float(ref_row["net_pnl_usdt"]) if ref_row else None,
                "net_winrate": float(ref_row["net_winrate"]) if ref_row else None,
            },
            "architecture_note": (
                "Canonical shared_strategy: candidates via evaluate_candidates_canonical "
                "(re-detect); entry = SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR; "
                "outcomes via simulate_canonical_trade (require_full_horizon=False, "
                "truncated→INCOMPLETE_OUTCOME_HORIZON). Frozen CSV is a reproducible "
                "artifact, not the sole strategy definition."
            ),
        },
        "n_ref_candidates": int(len(ref_cands)),
        "n_ref_trades": int(len(ref_trades)),
    }


def build_static_diff(repo: Path, sot: dict[str, Any]) -> list[dict[str, Any]]:
    """Phase 2: field-wise static comparison against shared canonical engine."""
    from .canonical_static import canonical_static_fields

    cells = frozen_cells_match_xrp_matrix_defs()
    fields = canonical_static_fields()
    fields.extend(
        [
            {
                "field": "window_start",
                "original_value": sot["selected"]["window"]["start"],
                "multicoin_value": DEFAULT_START.isoformat(),
                "match": _utc_iso(sot["selected"]["window"]["start"]) == DEFAULT_START.isoformat(),
                "source_file": "constants.py / core run_manifest.json",
                "function": "DEFAULT_START",
                "evidence": "Both claim 2026-07-24T00:00:00Z",
            },
            {
                "field": "window_end_exclusive",
                "original_value": sot["selected"]["window"]["end"],
                "multicoin_value": DEFAULT_END.isoformat(),
                "match": _utc_iso(sot["selected"]["window"]["end"]) == DEFAULT_END.isoformat(),
                "source_file": "constants.py / core run_manifest.json",
                "function": "DEFAULT_END",
                "evidence": "Both claim 2026-08-23T00:00:00Z exclusive",
            },
            {
                "field": "timeframe_primary",
                "original_value": "5m",
                "multicoin_value": PRIMARY_TF,
                "match": PRIMARY_TF == "5m",
                "source_file": "multicoin_frozen_validation/constants.py",
                "function": "PRIMARY_TF",
                "evidence": "Horizon matrix primary cell uses signal_timeframe=5m",
            },
            {
                "field": "mode_m0",
                "original_value": "M0_STRICT_SYNC",
                "multicoin_value": PRIMARY_MODE,
                "match": PRIMARY_MODE == "M0_STRICT_SYNC",
                "source_file": "constants.py",
                "function": "PRIMARY_MODE",
                "evidence": "detect_strict_sync_baseline / detect_cross_events",
            },
            {
                "field": "supportive_group",
                "original_value": "CORE_RESEARCH_SUPPORTIVE",
                "multicoin_value": PRIMARY_GROUP,
                "match": PRIMARY_GROUP == "CORE_RESEARCH_SUPPORTIVE",
                "source_file": "constants.py / core_sources_research_policy.py",
                "function": "apply_core_sources_research",
                "evidence": "AVAILABLE_CORE_SOURCES_RESEARCH_30D_V1",
            },
            {
                "field": "tp_sl_horizon_reference",
                "original_value": {"tp": 0.75, "sl": 0.50, "horizon": "8h"},
                "multicoin_value": {
                    "tp": next(c["tp_pct"] for c in PRIMARY_CELLS if c.get("is_reference")),
                    "sl": next(c["sl_pct"] for c in PRIMARY_CELLS if c.get("is_reference")),
                    "horizon": next(c["horizon"] for c in PRIMARY_CELLS if c.get("is_reference")),
                },
                "match": cells.get("reference_is_tp075_sl050_8h") is True,
                "source_file": "constants.PRIMARY_CELLS",
                "function": "PRIMARY_REFERENCE_CELL_ID",
                "evidence": PRIMARY_REFERENCE_CELL_ID,
            },
            {
                "field": "roundtrip_cost_pct",
                "original_value": 0.15,
                "multicoin_value": PRIMARY_COST_PCT,
                "match": float(PRIMARY_COST_PCT) == 0.15,
                "source_file": "shared_strategy/outcomes.py",
                "function": "simulate_canonical_trade",
                "evidence": "REF_COST_PCT=0.15",
            },
            {
                "field": "notional_usdt",
                "original_value": 1000.0,
                "multicoin_value": NOTIONAL_USDT,
                "match": float(NOTIONAL_USDT) == 1000.0,
                "source_file": "constants.py / shared semantics",
                "function": "REF_NOTIONAL",
                "evidence": "shared",
            },
            {
                "field": "same_bar_tp_sl_rule",
                "original_value": "SL_FIRST",
                "multicoin_value": "SL_FIRST",
                "match": True,
                "source_file": "tpsl_pnl_engine.simulate_tpsl_trade",
                "function": "simulate_tpsl_trade",
                "evidence": "Both paths call the same engine",
            },
            {
                "field": "shared_tpsl_engine",
                "original_value": "shared_strategy.simulate_canonical_trade",
                "multicoin_value": "shared_strategy.simulate_canonical_trade",
                "match": True,
                "source_file": "shared_strategy/outcomes.py",
                "function": "simulate_canonical_trade",
                "evidence": "Single outcome wrapper",
            },
        ]
    )
    for f in fields:
        f["match"] = bool(f["match"])
    return fields


def _metrics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades is None or trades.empty:
        return {
            "n_trades": 0,
            "tp": 0,
            "sl": 0,
            "time": 0,
            "winrate": None,
            "gross_pnl_usdt": None,
            "costs_usdt": None,
            "net_pnl_usdt": None,
            "expectancy_usdt": None,
            "profit_factor_net": None,
            "max_drawdown_usdt": None,
            "max_loss_streak": None,
        }
    reason = trades["exit_reason"].astype(str)
    pnl = pd.to_numeric(trades["net_pnl_usdt"], errors="coerce")
    gross = pd.to_numeric(trades.get("gross_pnl_usdt"), errors="coerce")
    costs = pd.to_numeric(trades.get("costs_usdt"), errors="coerce")
    wins = pnl > 0
    losses = pnl < 0
    gp = float(pnl[wins].sum()) if wins.any() else 0.0
    gl = float((-pnl[losses]).sum()) if losses.any() else 0.0
    pf = (gp / gl) if gl > 0 else (None if gp == 0 else float("inf"))
    # drawdown / streak
    eq = pnl.cumsum()
    dd = float((eq - eq.cummax()).min()) if len(eq) else 0.0
    streak = cur = 0
    for v in pnl.fillna(0):
        if v < 0:
            cur += 1
            streak = max(streak, cur)
        else:
            cur = 0
    return {
        "n_trades": int(len(trades)),
        "tp": int((reason == "TP_EXIT").sum()),
        "sl": int((reason == "SL_EXIT").sum()),
        "time": int(reason.isin(["TIME_EXIT", "HORIZON_EXIT"]).sum()),
        "winrate": float(wins.mean()),
        "gross_pnl_usdt": float(gross.sum()) if gross is not None else None,
        "costs_usdt": float(costs.sum()) if costs is not None else None,
        "net_pnl_usdt": float(pnl.sum()),
        "expectancy_usdt": float(pnl.mean()),
        "profit_factor_net": pf,
        "max_drawdown_usdt": abs(dd),
        "max_loss_streak": int(streak),
    }


def compare_candidates(original: pd.DataFrame, replay: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    o = original.copy()
    r = replay.copy()
    o["candidate_id"] = o["candidate_id"].astype(str)
    r["candidate_id"] = r["candidate_id"].astype(str)
    o_ids = set(o["candidate_id"])
    r_ids = set(r["candidate_id"])
    only_o = sorted(o_ids - r_ids)
    only_r = sorted(r_ids - o_ids)
    common = sorted(o_ids & r_ids)
    rows = []
    n_exact = 0
    n_dir = n_dec = n_ent = 0
    for cid in common:
        orow = o[o["candidate_id"] == cid].iloc[0]
        rrow = r[r["candidate_id"] == cid].iloc[0]
        diffs = []
        if str(orow.get("direction")).upper() != str(rrow.get("direction")).upper():
            diffs.append("direction")
            n_dir += 1
        if _utc_iso(orow.get("decision_at")) != _utc_iso(rrow.get("decision_at")):
            diffs.append("decision_at")
            n_dec += 1
        if _utc_iso(orow.get("entry_at")) != _utc_iso(rrow.get("entry_at")):
            diffs.append("entry_at")
            n_ent += 1
        if not _close(orow.get("entry_price"), rrow.get("entry_price"), PRICE_TOL):
            diffs.append("entry_price")
        if str(orow.get("core_research_verdict")) != str(rrow.get("core_research_verdict")):
            diffs.append("core_research_verdict")
        match = len(diffs) == 0
        if match:
            n_exact += 1
        rows.append(
            {
                "candidate_id": cid,
                "match": match,
                "diffs": "|".join(diffs),
                "original_direction": orow.get("direction"),
                "replay_direction": rrow.get("direction"),
                "original_decision_at": _utc_iso(orow.get("decision_at")),
                "replay_decision_at": _utc_iso(rrow.get("decision_at")),
                "original_entry_at": _utc_iso(orow.get("entry_at")),
                "replay_entry_at": _utc_iso(rrow.get("entry_at")),
                "original_entry_price": orow.get("entry_price"),
                "replay_entry_price": rrow.get("entry_price"),
                "original_verdict": orow.get("core_research_verdict"),
                "replay_verdict": rrow.get("core_research_verdict"),
            }
        )
    for cid in only_o:
        rows.append({"candidate_id": cid, "match": False, "diffs": "ONLY_ORIGINAL"})
    for cid in only_r:
        rows.append({"candidate_id": cid, "match": False, "diffs": "ONLY_REPLAY"})
    summary = {
        "n_original": len(o_ids),
        "n_replay": len(r_ids),
        "n_common": len(common),
        "n_exact_match": n_exact,
        "exact_match_rate": (n_exact / len(common)) if common else 0.0,
        "only_original": only_o,
        "only_replay": only_r,
        "direction_mismatch_count": n_dir,
        "decision_mismatch_count": n_dec,
        "entry_mismatch_count": n_ent,
        "duplicate_original": sum(1 for _, n in Counter(o["candidate_id"]).items() if n > 1),
        "duplicate_replay": sum(1 for _, n in Counter(r["candidate_id"]).items() if n > 1),
    }
    return pd.DataFrame(rows), summary


def compare_trades(original: pd.DataFrame, replay: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    o = original.copy()
    r = replay.copy()
    o["candidate_id"] = o["candidate_id"].astype(str)
    r["candidate_id"] = r["candidate_id"].astype(str)
    o_ids, r_ids = set(o["candidate_id"]), set(r["candidate_id"])
    only_o = sorted(o_ids - r_ids)
    only_r = sorted(r_ids - o_ids)
    common = sorted(o_ids & r_ids)
    rows = []
    mismatches = []
    n_exact = 0
    counts = Counter()
    for cid in common:
        orow = o[o["candidate_id"] == cid].iloc[0]
        rrow = r[r["candidate_id"] == cid].iloc[0]
        field_checks = [
            ("direction", str(orow.get("direction")).upper() == str(rrow.get("direction")).upper(), None),
            ("entry_at", _utc_iso(orow.get("entry_at")) == _utc_iso(rrow.get("entry_at")), None),
            ("entry_price", _close(orow.get("entry_price"), rrow.get("entry_price"), PRICE_TOL), PRICE_TOL),
            ("tp_price", _close(orow.get("tp_price"), rrow.get("tp_price"), PRICE_TOL), PRICE_TOL),
            ("sl_price", _close(orow.get("sl_price"), rrow.get("sl_price"), PRICE_TOL), PRICE_TOL),
            ("exit_at", _utc_iso(orow.get("exit_at")) == _utc_iso(rrow.get("exit_at")), None),
            ("exit_price", _close(orow.get("exit_price"), rrow.get("exit_price"), PRICE_TOL), PRICE_TOL),
            ("exit_reason", str(orow.get("exit_reason")) == str(rrow.get("exit_reason")), None),
            ("gross_pnl_usdt", _close(orow.get("gross_pnl_usdt"), rrow.get("gross_pnl_usdt"), PNL_TOL), PNL_TOL),
            ("costs_usdt", _close(orow.get("costs_usdt"), rrow.get("costs_usdt"), PNL_TOL), PNL_TOL),
            ("net_pnl_usdt", _close(orow.get("net_pnl_usdt"), rrow.get("net_pnl_usdt"), PNL_TOL), PNL_TOL),
        ]
        diffs = []
        for name, ok, _tol in field_checks:
            if not ok:
                diffs.append(name)
                counts[name] += 1
        match = len(diffs) == 0
        if match:
            n_exact += 1
        rec = {
            "candidate_id": cid,
            "match": match,
            "diffs": "|".join(diffs),
            "original_exit_reason": orow.get("exit_reason"),
            "replay_exit_reason": rrow.get("exit_reason"),
            "original_net_pnl_usdt": orow.get("net_pnl_usdt"),
            "replay_net_pnl_usdt": rrow.get("net_pnl_usdt"),
            "original_exit_at": _utc_iso(orow.get("exit_at")),
            "replay_exit_at": _utc_iso(rrow.get("exit_at")),
            "original_entry_at": _utc_iso(orow.get("entry_at")),
            "replay_entry_at": _utc_iso(rrow.get("entry_at")),
        }
        rows.append(rec)
        if not match:
            mismatches.append(rec)
    for cid in only_o:
        mismatches.append({"candidate_id": cid, "match": False, "diffs": "ONLY_ORIGINAL"})
    for cid in only_r:
        mismatches.append({"candidate_id": cid, "match": False, "diffs": "ONLY_REPLAY"})
    summary = {
        "n_original": len(o_ids),
        "n_replay": len(r_ids),
        "n_common": len(common),
        "n_exact_trade_match": n_exact,
        "exact_trade_match_rate": (n_exact / len(common)) if common else 0.0,
        "only_original": only_o,
        "only_replay": only_r,
        "direction_mismatch_count": counts["direction"],
        "entry_mismatch_count": counts["entry_at"] + counts["entry_price"],
        "exit_mismatch_count": counts["exit_at"] + counts["exit_price"] + counts["exit_reason"],
        "label_mismatch_count": counts["exit_reason"],
        "net_pnl_mismatch_count": counts["net_pnl_usdt"],
        "field_mismatch_counts": dict(counts),
        "price_tol": PRICE_TOL,
        "pnl_tol": PNL_TOL,
    }
    return pd.DataFrame(rows), pd.DataFrame(mismatches), summary


def run_multicoin_xrp_replay(repo: Path, out_dir: Path) -> dict[str, Any]:
    """Phase 3: isolated XRP replay via unchanged multicoin coin_backtest path."""
    from .....cluster_sweep_research.clickhouse_source import default_client

    out_dir.mkdir(parents=True, exist_ok=True)
    ck_dir = out_dir / "checkpoints"
    ck_dir.mkdir(parents=True, exist_ok=True)
    client = default_client()
    try:
        result = run_one_coin(
            client,
            symbol="XRPUSDT",
            start=DEFAULT_START,
            end=DEFAULT_END,
            coverage_class=None,
            window_report=None,
            cost_pct=PRIMARY_COST_PCT,
            repo=repo,
            enforce_xrp_parity=False,  # audit must obtain trades; parity compared separately
        )
    finally:
        if hasattr(client, "close"):
            client.close()

    # Atomic write replay checkpoint (do not touch multicoin_30d_frozen_validation)
    payload = {
        "schema_version": 1,
        "symbol": "XRPUSDT",
        "status": result.get("status"),
        "entry_rule": result.get("entry_rule"),
        "n_candidates": result.get("n_candidates"),
        "n_trades": result.get("n_trades"),
        "candidates": result.get("candidates"),
        "trades": result.get("trades"),
        "stats_by_strategy": result.get("stats_by_strategy"),
        "parity_gate_disabled_for_audit": True,
        "note": "Isolated strategy parity replay; not written into multicoin frozen validation outputs.",
    }
    path = ck_dir / "XRPUSDT.json"
    fd, tmp = tempfile.mkstemp(prefix=".XRPUSDT.", suffix=".tmp", dir=str(ck_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    atomic_write_json(out_dir / "run_manifest.json", {
        "mode": "multicoin_xrp_replay",
        "symbol": "XRPUSDT",
        "start": DEFAULT_START.isoformat(),
        "end": DEFAULT_END.isoformat(),
        "enforce_xrp_parity": False,
        "primary_reference_cell": PRIMARY_REFERENCE_CELL_ID,
        "entry_rule": ENTRY_RULE,
        "cost_pct": PRIMARY_COST_PCT,
        "status": result.get("status"),
        "n_candidates": result.get("n_candidates"),
        "n_trades": result.get("n_trades"),
    })
    return result


def filter_replay_reference(trades: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for t in trades:
        if (
            str(t.get("timeframe")) == PRIMARY_TF
            and str(t.get("mode_id")) == PRIMARY_MODE
            and str(t.get("group")) == PRIMARY_GROUP
            and str(t.get("strategy_key")) == PRIMARY_REFERENCE_CELL_ID
        ):
            rows.append(t)
    return pd.DataFrame(rows)


def decide_verdict(
    *,
    static_fields: list[dict[str, Any]],
    cand_sum: dict[str, Any],
    trade_sum: dict[str, Any],
) -> str:
    all_static = all(bool(f["match"]) for f in static_fields)
    cand_ok = (
        cand_sum.get("n_original") == cand_sum.get("n_replay")
        and cand_sum.get("n_exact_match") == cand_sum.get("n_common")
        and cand_sum.get("n_common", 0) > 0
        and not cand_sum.get("only_original")
        and not cand_sum.get("only_replay")
        and cand_sum.get("direction_mismatch_count", 0) == 0
        and cand_sum.get("decision_mismatch_count", 0) == 0
        and cand_sum.get("entry_mismatch_count", 0) == 0
    )
    trade_ok = (
        trade_sum.get("n_original") == trade_sum.get("n_replay")
        and trade_sum.get("n_exact_trade_match") == trade_sum.get("n_common")
        and trade_sum.get("n_common", 0) > 0
        and not trade_sum.get("only_original")
        and not trade_sum.get("only_replay")
        and trade_sum.get("direction_mismatch_count", 0) == 0
        and trade_sum.get("entry_mismatch_count", 0) == 0
        and trade_sum.get("exit_mismatch_count", 0) == 0
        and trade_sum.get("label_mismatch_count", 0) == 0
        and trade_sum.get("net_pnl_mismatch_count", 0) == 0
    )
    if all_static and cand_ok and trade_ok:
        return "MULTICOIN_BACKTESTER_USES_XRP_STRATEGY_1_TO_1_CONFIRMED"
    return "MULTICOIN_BACKTESTER_XRP_STRATEGY_PARITY_FAILED"


def run_audit(*, skip_replay: bool = False) -> dict[str, Any]:
    repo = _repo()
    audit_root = repo / AUDIT_ROOT_REL
    replay_dir = repo / REPLAY_REL
    audit_root.mkdir(parents=True, exist_ok=True)

    sot = identify_source_of_truth(repo)
    atomic_write_json(audit_root / "source_of_truth.json", {k: v for k, v in sot.items()})
    if sot["status"] != "OK":
        atomic_write_json(audit_root / "parity_summary.json", {
            "verdict": "XRP_SOURCE_OF_TRUTH_AMBIGUOUS",
            "source_of_truth": sot,
        })
        return {"verdict": "XRP_SOURCE_OF_TRUTH_AMBIGUOUS", "sot": sot}

    static_fields = build_static_diff(repo, sot)
    atomic_write_json(audit_root / "static_config_diff.json", {"fields": static_fields})
    atomic_write_json(
        audit_root / "static_code_path_audit.json",
        {
            "original_pnl_runner": "scripts/run_edc_xrp_horizon_tp_sl_matrix.py",
            "original_candidate_runner": "scripts/run_edc_xrp_30d_core_sources_comparison.py",
            "multicoin_runner": "scripts/run_edc_multicoin_frozen_validation.py",
            "multicoin_coin_path": "multicoin_frozen_validation/coin_backtest.run_one_coin",
            "shared_engine": "tolerance_research/tpsl_pnl_engine.py",
            "fields": static_fields,
            "note": "Hash equality is not used as sole evidence.",
        },
    )

    # Load original reference slices
    orig_cands = pd.read_csv(repo / ORIGINAL_CANDIDATES)
    orig_cands = orig_cands[
        (orig_cands["timeframe"] == "5m")
        & (orig_cands["mode_id"] == "M0_STRICT_SYNC")
        & (orig_cands["core_research_verdict"] == "CORE_RESEARCH_SUPPORTIVE")
    ].copy()
    orig_trades = pd.read_csv(repo / ORIGINAL_TRADES)
    orig_trades = orig_trades[
        (orig_trades["signal_timeframe"] == "5m")
        & (orig_trades["mode_id"] == "M0_STRICT_SYNC")
        & (orig_trades["group"] == "CORE_RESEARCH_SUPPORTIVE")
        & (orig_trades["strategy_id"] == "TP075_SL050")
        & (orig_trades["horizon"] == "8h")
    ].copy()

    if skip_replay:
        replay_ck = replay_dir / "checkpoints" / "XRPUSDT.json"
        if not replay_ck.exists():
            raise FileNotFoundError(f"Replay checkpoint missing: {replay_ck}")
        replay = json.loads(replay_ck.read_text(encoding="utf-8"))
    else:
        replay = run_multicoin_xrp_replay(repo, replay_dir)

    replay_cands_all = pd.DataFrame(replay.get("candidates") or [])
    if not replay_cands_all.empty:
        replay_cands = replay_cands_all[
            (replay_cands_all["timeframe"] == "5m")
            & (replay_cands_all["mode_id"] == "M0_STRICT_SYNC")
            & (replay_cands_all["core_research_verdict"] == "CORE_RESEARCH_SUPPORTIVE")
        ].copy()
    else:
        replay_cands = pd.DataFrame()

    replay_trades = filter_replay_reference(replay.get("trades") or [])

    cand_df, cand_sum = compare_candidates(orig_cands, replay_cands)
    trade_df, mismatch_df, trade_sum = compare_trades(orig_trades, replay_trades)

    orig_metrics = _metrics(orig_trades)
    replay_metrics = _metrics(replay_trades)
    orig_metrics["window"] = sot["selected"]["window"]
    replay_metrics["window"] = {"start": DEFAULT_START.isoformat(), "end": DEFAULT_END.isoformat()}
    orig_metrics["n_candidates"] = int(len(orig_cands))
    replay_metrics["n_candidates"] = int(len(replay_cands))

    verdict = decide_verdict(static_fields=static_fields, cand_sum=cand_sum, trade_sum=trade_sum)
    static_mismatches = [f for f in static_fields if not f["match"]]

    cand_df.to_csv(audit_root / "candidate_parity.csv", index=False)
    trade_df.to_csv(audit_root / "trade_parity.csv", index=False)
    if mismatch_df.empty:
        pd.DataFrame(
            columns=["candidate_id", "match", "diffs", "original_exit_reason", "replay_exit_reason"]
        ).to_csv(audit_root / "mismatches.csv", index=False)
    else:
        mismatch_df.to_csv(audit_root / "mismatches.csv", index=False)

    # MFE/MAE not present on original trades_matrix SoT
    mfe_mae_note = (
        "MFE/MAE columns absent from original trades_matrix.csv; "
        "row parity for MFE/MAE is therefore not provable from SoT and is not used for the verdict."
    )

    summary = {
        "verdict": verdict,
        "root_cause": (
            None
            if verdict == "MULTICOIN_BACKTESTER_USES_XRP_STRATEGY_1_TO_1_CONFIRMED"
            else (
                "Residual static or row mismatches after shared-engine unification; "
                f"static_mismatches={ [f['field'] for f in static_mismatches] }"
            )
        ),
        "unification_note": (
            "Both paths use shared_strategy (SIGNAL_TF_NEXT_OPEN, require_full_horizon=False "
            "with truncated→INCOMPLETE, warm=5d/outcome=12h, scoped XRP parity gate)."
        ),
        "mfe_mae_note": mfe_mae_note,
        "source_of_truth": sot["selected"],
        "static_mismatch_count": len(static_mismatches),
        "static_mismatches": [f["field"] for f in static_mismatches],
        "original_metrics": orig_metrics,
        "replay_metrics": replay_metrics,
        "candidate_parity": cand_sum,
        "trade_parity": trade_sum,
        "first_20_mismatches": mismatch_df.head(20).to_dict(orient="records") if not mismatch_df.empty else [],
        "replay_path": str(replay_dir),
        "price_tol": PRICE_TOL,
        "pnl_tol": PNL_TOL,
    }
    atomic_write_json(audit_root / "parity_summary.json", summary)

    md = [
        "# XRP strategy 1:1 parity audit",
        "",
        f"**Verdict:** `{verdict}`",
        "",
        "## Source of truth",
        f"- Candidates: `{ORIGINAL_CANDIDATES}`",
        f"- Trades: `{ORIGINAL_TRADES}`",
        f"- Reference cell: 5m M0 SUPPORTIVE TP075/SL050/8h cost 0.15%",
        f"- Original n_trades={orig_metrics['n_trades']} net_pnl={orig_metrics['net_pnl_usdt']}",
        "",
        "## Static mismatches",
    ]
    for f in static_mismatches:
        md.append(f"- **{f['field']}**: original=`{f['original_value']}` vs multicoin=`{f['multicoin_value']}` ({f['evidence']})")
    md += [
        "",
        "## Root cause (no repair applied)",
        summary["root_cause"] or "(none — parity confirmed)",
        "",
        f"## MFE/MAE\n{mfe_mae_note}",
        "",
        "## Metrics",
        f"- Original: {json.dumps(orig_metrics)}",
        f"- Replay: {json.dumps(replay_metrics)}",
        "",
        "## Row parity",
        f"- Candidates exact: {cand_sum.get('n_exact_match')}/{cand_sum.get('n_common')} "
        f"(only_o={len(cand_sum.get('only_original') or [])}, only_r={len(cand_sum.get('only_replay') or [])})",
        f"- Trades exact: {trade_sum.get('n_exact_trade_match')}/{trade_sum.get('n_common')} "
        f"(only_o={len(trade_sum.get('only_original') or [])}, only_r={len(trade_sum.get('only_replay') or [])})",
        "",
        "## First mismatches",
    ]
    for rec in summary["first_20_mismatches"][:20]:
        md.append(f"- `{rec.get('candidate_id')}`: {rec.get('diffs')}")
    (audit_root / "parity_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    return summary


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--skip-replay", action="store_true")
    args = p.parse_args()
    out = run_audit(skip_replay=args.skip_replay)
    print("verdict:", out.get("verdict"))
    print(json.dumps({k: out.get(k) for k in ("static_mismatch_count", "candidate_parity", "trade_parity", "original_metrics", "replay_metrics")}, indent=2, default=str))
