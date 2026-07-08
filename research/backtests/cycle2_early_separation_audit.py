from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from research.backtests.candle_loader import (
    DEFAULT_DATA_DIR,
    load_candles_for_symbol,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "research" / "backtests" / "results"

BASELINE_DIRS = [
    "live_relief_cycle3_long_window",
    "live_short_tp_relief_long_window",
]

OUT_DIR = (
    RESULTS_ROOT
    / "cycle2_early_separation_audit"
)

# Beobachtungshorizonte (Offset-Candles nach C2)
CHECKPOINT_OFFSETS = [1, 3, 5, 10, 20, 30, 50, 75, 100, 150, 250]
# Für Kandidatenmatrix konzentrieren wir uns auf:
CANDIDATE_OFFSETS = [5, 10, 20, 30, 50, 100]


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
    # pragma: no cover - defensive
        return None


@dataclass
class C2Trade:
    dir_name: str
    tb_path: Path
    symbol: str
    direction: str
    start_index: int
    start_time: str
    candles_processed: int
    final_status: str
    final_overall_pnl: float
    exit_reason: str
    trade_block_id: str
    c2_candle_index: int
    c2_timestamp: str
    long_qty_at_c2: float
    short_qty_at_c2: float
    long_avg_at_c2: float
    short_avg_at_c2: float
    realized_pnl_at_c2: float
    overall_pnl_at_c2: float
    close_at_c2: float
    net_qty_at_c2: float
    ratio_at_c2: float
    position_notional_at_c2: float
    has_c3_long_fill: bool
    has_c3_short_fill: bool
    c3_complete: bool


@dataclass
class CheckpointRow:
    # Identifikation
    dir_name: str
    start_index: int
    trade_block_id: str
    classification_outcome: str  # "GOOD" oder "STUCK"
    checkpoint_offset: int
    checkpoint_candle_index: Optional[int]
    # Preisbewegung
    close_at_c2: Optional[float]
    close_at_checkpoint: Optional[float]
    price_change_from_c2_pct: Optional[float]
    highest_price_since_c2: Optional[float]
    lowest_price_since_c2: Optional[float]
    max_rebound_from_c2_pct: Optional[float]
    max_drop_from_c2_pct: Optional[float]
    rebound_from_post_c2_low_pct: Optional[float]
    candle_index_of_post_c2_low: Optional[int]
    candle_index_of_best_rebound: Optional[int]
    # Freeze-PnL
    freeze_pnl_at_checkpoint: Optional[float]
    freeze_pnl_change_from_c2: Optional[float]
    freeze_best_pnl_until_checkpoint: Optional[float]
    freeze_worst_pnl_until_checkpoint: Optional[float]
    freeze_drawdown_from_best: Optional[float]
    freeze_break_even_reached_until_checkpoint: Optional[bool]
    candle_of_freeze_break_even: Optional[int]
    freeze_recovery_to_minus_0_25_reached: Optional[bool]
    freeze_recovery_to_minus_0_10_reached: Optional[bool]
    # Distanz / Struktur
    distance_to_long_avg_pct: Optional[float]
    distance_to_short_avg_pct: Optional[float]
    net_long_qty_at_c2: Optional[float]
    position_notional_at_c2: Optional[float]
    long_short_ratio_at_c2: Optional[float]
    projected_cycle3_long_qty: Optional[float]
    projected_long_qty_after_cycle3: Optional[float]
    projected_net_long_after_cycle3: Optional[float]
    projected_notional_after_cycle3: Optional[float]
    projected_notional_increase_pct: Optional[float]
    # Tatsächlicher späterer Ausgang (pro Trade konstant, aber der Vollständigkeit halber hier)
    trade_outcome: str
    baseline_closed: bool
    baseline_final_pnl: float
    baseline_exit_reason: str
    cycle3_long_filled: bool
    cycle3_short_filled: bool
    cycle3_complete: bool
    stuck_after_c2: bool


def _load_multi_start_runs(base_dir: Path) -> Dict[int, Dict[str, Any]]:
    ms_path = base_dir / "APTUSDT_original_hedge_5m_multi_start_results.json"
    if not ms_path.is_file():
        return {}
    payload = json.loads(ms_path.read_text(encoding="utf-8"))
    runs = payload.get("runs") or []
    by_start: Dict[int, Dict[str, Any]] = {}
    for run in runs:
        si = int(run.get("start_index") or 0)
        by_start[si] = run
    return by_start


def _iter_c2_trades() -> List[C2Trade]:
    """Sammle alle Baseline-Trades mit CYCLE_2_SHORT_REDUCE aus den Basisverzeichnissen."""
    trades: List[C2Trade] = []
    # zur Duplikat-Erkennung: (symbol, direction, start_index, start_time)
    seen_keys: set[Tuple[Any, Any, Any, Any]] = set()

    for dir_name in BASELINE_DIRS:
        base_dir = RESULTS_ROOT / dir_name
        if not base_dir.is_dir():
            continue
        runs_by_start = _load_multi_start_runs(base_dir)
        if not runs_by_start:
            continue

        for tb_path in base_dir.glob("APTUSDT_long_start*_conservative_live_trade_blocks.json"):
            data = json.loads(tb_path.read_text(encoding="utf-8"))
            meta = data.get("metadata") or {}
            start_index = int(meta.get("start_index") or 0)
            run = runs_by_start.get(start_index)
            if not run:
                continue

            symbol = str(run.get("symbol") or "APTUSDT")
            direction = str(run.get("direction") or "long")
            start_time = str(run.get("start_time") or "")

            key = (symbol, direction, start_index, start_time)
            if key in seen_keys:
                # Duplikat desselben Trades in anderem Verzeichnis – überspringen.
                continue
            seen_keys.add(key)

            trades_rows: List[Dict[str, Any]] = list(data.get("trade_blocks") or [])
            c2_rows = [
                row
                for row in trades_rows
                if str(row.get("row_type") or "") == "fill"
                and str(row.get("purpose") or row.get("purpose_original") or "")
                == "CYCLE_2_SHORT_REDUCE"
            ]
            if not c2_rows:
                continue
            # Letzten C2-Fill des Trades nehmen.
            c2 = c2_rows[-1]
            c2_ci = int(c2.get("candle_index") or 0)
            c2_ts = str(c2.get("timestamp") or "")
            l_after = float(c2.get("long_qty_after") or 0.0)
            s_after = float(c2.get("short_qty_after") or 0.0)
            la = float(c2.get("long_avg_after") or 0.0)
            sa = float(c2.get("short_avg_after") or 0.0)
            realized_cum = float(c2.get("cumulative_pnl") or 0.0)

            # Candle-Fenster rekonstruieren.
            candles_processed = int(run.get("candles_processed") or 0)
            all_rows = load_candles_for_symbol(
                symbol,
                timeframe="5m",
                data_dir=DEFAULT_DATA_DIR,
                limit=60000,
            )
            start_idx = None
            for idx, row in enumerate(all_rows):
                ts = row.get("timestamp")
                if ts is None:
                    continue
                if ts.isoformat() == start_time:
                    start_idx = idx
                    break
            if start_idx is None:
                continue
            window = all_rows[start_idx : start_idx + candles_processed]
            if c2_ci >= len(window):
                continue

            close_c2 = float(window[c2_ci]["close"])
            long_unr_c2 = (close_c2 - la) * l_after
            short_unr_c2 = (sa - close_c2) * s_after
            unr_c2 = long_unr_c2 + short_unr_c2
            overall_c2 = realized_cum + unr_c2
            net = l_after - s_after
            ratio = l_after / s_after if abs(s_after) > 1e-12 else 0.0
            notional_c2 = (abs(l_after) + abs(s_after)) * close_c2

            # Cycle-3-Fills bestimmen.
            has_c3_long = any(
                str(row.get("row_type") or "") == "fill"
                and str(row.get("purpose") or row.get("purpose_original") or "")
                == "CYCLE_3_LONG_ADD"
                for row in trades_rows
            )
            has_c3_short = any(
                str(row.get("row_type") or "") == "fill"
                and str(row.get("purpose") or row.get("purpose_original") or "")
                == "CYCLE_3_SHORT_REDUCE"
                for row in trades_rows
            )
            c3_complete = has_c3_long and has_c3_short

            final_status = str(run.get("final_status") or "")
            final_overall = float(run.get("overall_pnl") or 0.0)

            trades.append(
                C2Trade(
                    dir_name=dir_name,
                    tb_path=tb_path,
                    symbol=symbol,
                    direction=direction,
                    start_index=start_index,
                    start_time=start_time,
                    candles_processed=candles_processed,
                    final_status=final_status,
                    final_overall_pnl=final_overall,
                    exit_reason=str(run.get("exit_reason") or ""),
                    trade_block_id=str(run.get("final_strategy_state_excerpt", {}).get("trade_block_id") or "backtest_long_start0"),
                    c2_candle_index=c2_ci,
                    c2_timestamp=c2_ts,
                    long_qty_at_c2=l_after,
                    short_qty_at_c2=s_after,
                    long_avg_at_c2=la,
                    short_avg_at_c2=sa,
                    realized_pnl_at_c2=realized_cum,
                    overall_pnl_at_c2=overall_c2,
                    close_at_c2=close_c2,
                    net_qty_at_c2=net,
                    ratio_at_c2=ratio,
                    position_notional_at_c2=notional_c2,
                    has_c3_long_fill=has_c3_long,
                    has_c3_short_fill=has_c3_short,
                    c3_complete=c3_complete,
                )
            )

    return trades


def _build_checkpoint_rows(trades: List[C2Trade]) -> List[CheckpointRow]:
    rows: List[CheckpointRow] = []

    # Preload candle windows to vermeiden mehrfaches Laden.
    window_cache: Dict[Tuple[str, str, int, str], List[Dict[str, Any]]] = {}

    for tr in trades:
        key = (tr.symbol, tr.direction, tr.start_index, tr.start_time)
        if key not in window_cache:
            all_rows = load_candles_for_symbol(
                tr.symbol,
                timeframe="5m",
                data_dir=DEFAULT_DATA_DIR,
                limit=60000,
            )
            start_idx = None
            for idx, row in enumerate(all_rows):
                ts = row.get("timestamp")
                if ts is None:
                    continue
                if ts.isoformat() == tr.start_time:
                    start_idx = idx
                    break
            if start_idx is None:
                continue
            window_cache[key] = all_rows[start_idx : start_idx + tr.candles_processed]
        window = window_cache[key]
        n = len(window)

        c2_ci = tr.c2_candle_index
        if c2_ci >= n:
            continue

        closes = [float(w["close"]) for w in window]

        # Freeze-PnL Serie ab C2
        freeze_pnls: List[float] = []
        la = tr.long_avg_at_c2
        sa = tr.short_avg_at_c2
        lq = tr.long_qty_at_c2
        sq = tr.short_qty_at_c2
        realized_c2 = tr.realized_pnl_at_c2
        for ci in range(c2_ci, n):
            price = closes[ci]
            long_unr = (price - la) * lq
            short_unr = (sa - price) * sq
            freeze_pnls.append(realized_c2 + long_unr + short_unr)

        # Outcome-Klassifikation (GOOD vs STUCK)
        closed = (
            tr.final_status == "closed"
            and abs(tr.final_overall_pnl) >= 0.0  # immer wahr, zur Klarheit
        )
        positive = closed and tr.final_overall_pnl > 0.0
        outcome = "GOOD" if positive else "STUCK"
        stuck_after_c2 = (not closed) or (tr.final_overall_pnl <= 0.0)

        # Projektion Cycle 3 Long-Menge (Intent aus Trade-Blocks, falls vorhanden)
        projected_c3_long_qty: Optional[float] = None
        projected_long_after_c3: Optional[float] = None
        projected_net_after_c3: Optional[float] = None
        projected_notional_after_c3: Optional[float] = None
        projected_notional_increase_pct: Optional[float] = None
        try:
            tb_data = json.loads(tr.tb_path.read_text(encoding="utf-8"))
            tb_rows: List[Dict[str, Any]] = list(tb_data.get("trade_blocks") or [])
            # erste Intent-Zeile nach C2 mit CYCLE_3_LONG_ADD
            c3_intent = next(
                (
                    row
                    for row in tb_rows
                    if str(row.get("row_type") or "") == "intent"
                    and int(row.get("candle_index") or 0) >= c2_ci
                    and str(row.get("purpose") or row.get("purpose_original") or "")
                    == "CYCLE_3_LONG_ADD"
                ),
                None,
            )
            if c3_intent is not None:
                projected_c3_long_qty = _safe_float(c3_intent.get("qty")) or 0.0
                projected_long_after_c3 = tr.long_qty_at_c2 - projected_c3_long_qty
                projected_net_after_c3 = projected_long_after_c3 - tr.short_qty_at_c2
                projected_notional_after_c3 = (
                    abs(projected_long_after_c3) + abs(tr.short_qty_at_c2)
                ) * tr.close_at_c2
                if tr.position_notional_at_c2 > 1e-12:
                    projected_notional_increase_pct = (
                        (projected_notional_after_c3 - tr.position_notional_at_c2)
                        / tr.position_notional_at_c2
                        * 100.0
                    )
        except Exception:
            pass

        for offset in CHECKPOINT_OFFSETS:
            cp_ci = c2_ci + offset
            if cp_ci >= n:
                # Checkpoint außerhalb des Fensters – NA-Zeile erzeugen.
                rows.append(
                    CheckpointRow(
                        dir_name=tr.dir_name,
                        start_index=tr.start_index,
                        trade_block_id=tr.trade_block_id,
                        classification_outcome=outcome,
                        checkpoint_offset=offset,
                        checkpoint_candle_index=None,
                        close_at_c2=tr.close_at_c2,
                        close_at_checkpoint=None,
                        price_change_from_c2_pct=None,
                        highest_price_since_c2=None,
                        lowest_price_since_c2=None,
                        max_rebound_from_c2_pct=None,
                        max_drop_from_c2_pct=None,
                        rebound_from_post_c2_low_pct=None,
                        candle_index_of_post_c2_low=None,
                        candle_index_of_best_rebound=None,
                        freeze_pnl_at_checkpoint=None,
                        freeze_pnl_change_from_c2=None,
                        freeze_best_pnl_until_checkpoint=None,
                        freeze_worst_pnl_until_checkpoint=None,
                        freeze_drawdown_from_best=None,
                        freeze_break_even_reached_until_checkpoint=None,
                        candle_of_freeze_break_even=None,
                        freeze_recovery_to_minus_0_25_reached=None,
                        freeze_recovery_to_minus_0_10_reached=None,
                        distance_to_long_avg_pct=None,
                        distance_to_short_avg_pct=None,
                        net_long_qty_at_c2=tr.net_qty_at_c2,
                        position_notional_at_c2=tr.position_notional_at_c2,
                        long_short_ratio_at_c2=tr.ratio_at_c2,
                        projected_cycle3_long_qty=projected_c3_long_qty,
                        projected_long_qty_after_cycle3=projected_long_after_c3,
                        projected_net_long_after_cycle3=projected_net_after_c3,
                        projected_notional_after_cycle3=projected_notional_after_c3,
                        projected_notional_increase_pct=projected_notional_increase_pct,
                        trade_outcome=outcome,
                        baseline_closed=closed,
                        baseline_final_pnl=tr.final_overall_pnl,
                        baseline_exit_reason=tr.exit_reason,
                        cycle3_long_filled=tr.has_c3_long_fill,
                        cycle3_short_filled=tr.has_c3_short_fill,
                        cycle3_complete=tr.c3_complete,
                        stuck_after_c2=stuck_after_c2,
                    )
                )
                continue

            # Preis-Metriken
            close_cp = closes[cp_ci]
            close_c2 = tr.close_at_c2
            price_change_pct = (close_cp - close_c2) / close_c2 * 100.0
            window_slice = closes[c2_ci : cp_ci + 1]
            highest = max(window_slice)
            lowest = min(window_slice)
            max_rebound_pct = (highest - close_c2) / close_c2 * 100.0
            max_drop_pct = (lowest - close_c2) / close_c2 * 100.0
            # Post-C2-Low und bester Rebound
            post_low_price = lowest
            post_low_rel_index = window_slice.index(post_low_price)
            post_low_ci = c2_ci + post_low_rel_index
            best_rebound_price = max(window_slice[post_low_rel_index:])
            best_rebound_rel_index = window_slice[post_low_rel_index:].index(
                best_rebound_price
            )
            best_rebound_ci = post_low_ci + best_rebound_rel_index
            rebound_from_low_pct = (
                (best_rebound_price - post_low_price) / post_low_price * 100.0
                if post_low_price > 0
                else 0.0
            )

            # Freeze-PnL-Metriken
            idx0 = 0  # Index von c2_ci im freeze_pnls
            idx_cp = cp_ci - c2_ci
            freeze_at_c2 = freeze_pnls[idx0]
            freeze_at_cp = freeze_pnls[idx_cp]
            freeze_best = max(freeze_pnls[idx0 : idx_cp + 1])
            freeze_worst = min(freeze_pnls[idx0 : idx_cp + 1])
            freeze_drawdown = freeze_best - freeze_at_cp
            # Break-even & Schwellen
            candle_of_be: Optional[int] = None
            be_reached = False
            rec_025 = False
            rec_010 = False
            for rel_ci, pnl in enumerate(freeze_pnls[idx0 : idx_cp + 1]):
                abs_ci = c2_ci + rel_ci
                if not be_reached and pnl >= 0.0:
                    be_reached = True
                    candle_of_be = abs_ci
                if pnl >= -0.25:
                    rec_025 = True
                if pnl >= -0.10:
                    rec_010 = True

            # Distanz zu Averages
            dist_long_pct = (close_cp - tr.long_avg_at_c2) / tr.long_avg_at_c2 * 100.0
            dist_short_pct = (tr.short_avg_at_c2 - close_cp) / tr.short_avg_at_c2 * 100.0

            rows.append(
                CheckpointRow(
                    dir_name=tr.dir_name,
                    start_index=tr.start_index,
                    trade_block_id=tr.trade_block_id,
                    classification_outcome=outcome,
                    checkpoint_offset=offset,
                    checkpoint_candle_index=cp_ci,
                    close_at_c2=close_c2,
                    close_at_checkpoint=close_cp,
                    price_change_from_c2_pct=price_change_pct,
                    highest_price_since_c2=highest,
                    lowest_price_since_c2=lowest,
                    max_rebound_from_c2_pct=max_rebound_pct,
                    max_drop_from_c2_pct=max_drop_pct,
                    rebound_from_post_c2_low_pct=rebound_from_low_pct,
                    candle_index_of_post_c2_low=post_low_ci,
                    candle_index_of_best_rebound=best_rebound_ci,
                    freeze_pnl_at_checkpoint=freeze_at_cp,
                    freeze_pnl_change_from_c2=freeze_at_cp - freeze_at_c2,
                    freeze_best_pnl_until_checkpoint=freeze_best,
                    freeze_worst_pnl_until_checkpoint=freeze_worst,
                    freeze_drawdown_from_best=freeze_drawdown,
                    freeze_break_even_reached_until_checkpoint=be_reached,
                    candle_of_freeze_break_even=candle_of_be,
                    freeze_recovery_to_minus_0_25_reached=rec_025,
                    freeze_recovery_to_minus_0_10_reached=rec_010,
                    distance_to_long_avg_pct=dist_long_pct,
                    distance_to_short_avg_pct=dist_short_pct,
                    net_long_qty_at_c2=tr.net_qty_at_c2,
                    position_notional_at_c2=tr.position_notional_at_c2,
                    long_short_ratio_at_c2=tr.ratio_at_c2,
                    projected_cycle3_long_qty=projected_c3_long_qty,
                    projected_long_qty_after_cycle3=projected_long_after_c3,
                    projected_net_long_after_cycle3=projected_net_after_c3,
                    projected_notional_after_cycle3=projected_notional_after_c3,
                    projected_notional_increase_pct=projected_notional_increase_pct,
                    trade_outcome=outcome,
                    baseline_closed=closed,
                    baseline_final_pnl=tr.final_overall_pnl,
                    baseline_exit_reason=tr.exit_reason,
                    cycle3_long_filled=tr.has_c3_long_fill,
                    cycle3_short_filled=tr.has_c3_short_fill,
                    cycle3_complete=tr.c3_complete,
                    stuck_after_c2=stuck_after_c2,
                )
            )

    return rows


def _write_checkpoint_metrics(path: Path, rows: List[CheckpointRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = [field.name for field in CheckpointRow.__dataclass_fields__.values()]  # type: ignore[attr-defined]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r.__dict__)


def _build_candidate_rules(rows: List[CheckpointRow]) -> List[Dict[str, Any]]:
    """Einfache, nachvollziehbare Kandidatenregeln ohne Sweep.

    Für jeden Horizon und jede Metrik berechnen wir:
    - Wert des GOOD-Trades
    - Min/Max der STUCK-Trades
    - Ob eine saubere Trennung existiert
    - Eine grobe Margin-Einschätzung (ROBUST / WEAK_MARGIN / NO_SEPARATION).
    """
    result: List[Dict[str, Any]] = []

    for offset in CANDIDATE_OFFSETS:
        subset = [r for r in rows if r.checkpoint_offset == offset]
        goods = [r for r in subset if r.classification_outcome == "GOOD"]
        stucks = [r for r in subset if r.classification_outcome == "STUCK"]
        if not goods or not stucks:
            continue
        g = goods[0]  # es gibt genau einen GOOD-Trade (Start 9000)

        def _eval_metric(name: str, higher_is_better: bool) -> Dict[str, Any]:
            g_val = getattr(g, name)
            s_vals = [getattr(s, name) for s in stucks if getattr(s, name) is not None]
            if g_val is None or not s_vals:
                return {
                    "checkpoint_offset": offset,
                    "metric": name,
                    "good_value": None,
                    "stuck_min": None,
                    "stuck_max": None,
                    "separation": False,
                    "margin": None,
                    "assessment": "NO_DATA",
                }
            g_f = float(g_val)
            s_min = float(min(s_vals))
            s_max = float(max(s_vals))
            separation = False
            margin = 0.0
            if higher_is_better:
                # GOOD klar besser, wenn > max STUCK
                if g_f > s_max:
                    separation = True
                    margin = g_f - s_max
            else:
                # GOOD klar besser, wenn < min STUCK
                if g_f < s_min:
                    separation = True
                    margin = s_min - g_f
            if not separation:
                assessment = "NO_SEPARATION"
            else:
                # Margin-Qualität grob beurteilen
                ref = abs(g_f) if abs(g_f) > 1e-6 else 1.0
                rel = margin / ref
                if margin > 0.1 and rel > 0.1:
                    assessment = "ROBUST"
                else:
                    assessment = "WEAK_MARGIN"
            return {
                "checkpoint_offset": offset,
                "metric": name,
                "good_value": g_f,
                "stuck_min": s_min,
                "stuck_max": s_max,
                "separation": separation,
                "margin": margin,
                "assessment": assessment,
            }

        # Rebound-Kandidat
        result.append(_eval_metric("max_rebound_from_c2_pct", higher_is_better=True))
        # Verlust-Kandidat (stärkerer Drop = schlechter)
        result.append(_eval_metric("max_drop_from_c2_pct", higher_is_better=False))
        # PnL-Kandidat (bester Freeze-PnL)
        result.append(
            _eval_metric("freeze_best_pnl_until_checkpoint", higher_is_better=True)
        )
        # Distanz zum Long-Average (weniger Distanz ist tendenziell besser -> closer to 0)
        result.append(
            _eval_metric("distance_to_long_avg_pct", higher_is_better=False)
        )

    return result


def _write_candidate_rules(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def _write_summary_and_md(
    summary_path: Path,
    md_path: Path,
    trades: List[C2Trade],
    checkpoints: List[CheckpointRow],
    candidates: List[Dict[str, Any]],
) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    total_trades = len(trades)
    good_trades = [t for t in trades if t.final_status == "closed" and t.final_overall_pnl > 0.0]
    stuck_trades = [t for t in trades if not (t.final_status == "closed" and t.final_overall_pnl > 0.0)]

    # Früheste Separation: wir suchen den kleinsten Offset, an dem es für mind. eine Metrik
    # eine robuste oder schwache, aber vorhandene Trennung gibt.
    earliest_sep_offset: Optional[int] = None
    earliest_metric: Optional[str] = None
    earliest_assessment: Optional[str] = None
    for offset in sorted(CANDIDATE_OFFSETS):
        for row in candidates:
            if row["checkpoint_offset"] != offset:
                continue
            if not row["separation"]:
                continue
            earliest_sep_offset = offset
            earliest_metric = str(row["metric"])
            earliest_assessment = str(row["assessment"])
            break
        if earliest_sep_offset is not None:
            break

    summary = {
        "total_c2_trades": total_trades,
        "good_trades": len(good_trades),
        "stuck_trades": len(stuck_trades),
        "earliest_separation_offset": earliest_sep_offset,
        "earliest_separation_metric": earliest_metric,
        "earliest_separation_assessment": earliest_assessment,
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    with md_path.open("w", encoding="utf-8") as handle:
        handle.write("# CYCLE_2_SHORT_REDUCE Early-Separation-Audit\n\n")
        handle.write(f"- Anzahl untersuchter C2-Trades: {total_trades}\n")
        handle.write(f"- GOOD-Trades (später positiv geschlossen): {len(good_trades)}\n")
        handle.write(f"- STUCK-Trades (später nicht positiv): {len(stuck_trades)}\n")
        handle.write(
            f"- Früheste sichtbare Trennung (wenn vorhanden): "
            f"Offset={earliest_sep_offset}, Metrik={earliest_metric}, "
            f"Bewertung={earliest_assessment}\n"
        )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    trades = _iter_c2_trades()
    if not trades:
        # Nichts zu tun – leere Dateien schreiben.
        _write_checkpoint_metrics(OUT_DIR / "cycle2_checkpoint_metrics.csv", [])
        _write_candidate_rules(OUT_DIR / "cycle2_candidate_rules.csv", [])
        (OUT_DIR / "cycle2_good_vs_stuck_comparison.csv").write_text("", encoding="utf-8")
        (OUT_DIR / "cycle2_early_separation_summary.json").write_text(
            json.dumps({"total_c2_trades": 0}, indent=2),
            encoding="utf-8",
        )
        (OUT_DIR / "cycle2_early_separation_diagnosis.md").write_text(
            "# CYCLE_2_SHORT_REDUCE Early-Separation-Audit\n\nKeine C2-Trades gefunden.\n",
            encoding="utf-8",
        )
        return 0

    checkpoint_rows = _build_checkpoint_rows(trades)
    _write_checkpoint_metrics(OUT_DIR / "cycle2_checkpoint_metrics.csv", checkpoint_rows)

    candidate_rows = _build_candidate_rules(checkpoint_rows)
    _write_candidate_rules(OUT_DIR / "cycle2_candidate_rules.csv", candidate_rows)

    # Für einen knappen GOOD-vs-STUCK-Vergleich können Nutzer direkt die
    # Checkpoint-Metriken filtern; hier erzeugen wir keine zusätzliche,
    # abgeleitete CSV mehr.
    (OUT_DIR / "cycle2_good_vs_stuck_comparison.csv").write_text(
        "Use cycle2_checkpoint_metrics.csv and filter by classification_outcome.\n",
        encoding="utf-8",
    )

    _write_summary_and_md(
        OUT_DIR / "cycle2_early_separation_summary.json",
        OUT_DIR / "cycle2_early_separation_diagnosis.md",
        trades,
        checkpoint_rows,
        candidate_rows,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

