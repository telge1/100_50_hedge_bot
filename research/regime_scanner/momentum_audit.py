"""Momentum audit harness over PriceActionConfirmations (research-only).

Walks closed 5m candles after each PA confirmation. No entry / TP.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from .indicators import atr_wilder
from .momentum import (
    MomentumConfig,
    default_momentum_config,
    evaluate_momentum_confirmation,
    initialize_momentum_state,
    update_momentum_state,
)
from .point_audit import json_safe


def _ts_str(value: object) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return str(ts.isoformat())


def _rolling_median_volume(volumes: pd.Series, index: int, window: int = 20) -> float | None:
    start = max(0, int(index) - int(window) + 1)
    chunk = pd.to_numeric(volumes.iloc[start : int(index) + 1], errors="coerce")
    med = float(chunk.median()) if len(chunk) else float("nan")
    if med != med or med <= 0:
        return None
    return med


def run_momentum_audit(
    *,
    price_action_confirmations: list[dict[str, Any]],
    candles: pd.DataFrame,
    momentum_config: MomentumConfig | None = None,
    setup_activations: list[dict[str, Any]] | None = None,
    atr_period: int = 14,
    volume_median_window: int = 20,
) -> dict[str, Any]:
    """Evaluate MomentumConfirmation for each PA confirmation on 5m candles."""
    cfg = momentum_config or default_momentum_config()
    frame = candles.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    ts_list = [_ts_str(t) for t in frame["timestamp"].tolist()]
    ts_to_i = {t: i for i, t in enumerate(ts_list)}

    atr_series = atr_wilder(
        pd.to_numeric(frame["high"], errors="coerce"),
        pd.to_numeric(frame["low"], errors="coerce"),
        pd.to_numeric(frame["close"], errors="coerce"),
        int(atr_period),
    )
    volumes = frame["volume"] if "volume" in frame.columns else pd.Series([None] * len(frame))

    setups = list(setup_activations or [])
    setups_by_ts: dict[str, list[dict[str, Any]]] = {}
    for s in setups:
        key = _ts_str(s.get("setup_activation_timestamp"))
        setups_by_ts.setdefault(key, []).append(s)

    event_rows: list[dict[str, Any]] = []
    confirmation_rows: list[dict[str, Any]] = []
    diagnostic_rows: list[dict[str, Any]] = []
    detail_cases: dict[str, Any] = {
        "confirmed_on_break_candle": None,
        "confirmed_later": None,
        "not_confirmed": None,
    }

    for pa in price_action_confirmations:
        break_ts = pa.get("structure_break_timestamp")
        if break_ts is None:
            continue
        break_key = _ts_str(break_ts)
        if break_key not in ts_to_i:
            continue
        i0 = ts_to_i[break_key]
        state = initialize_momentum_state(pa, cfg)
        for ev in state.get("event_log") or []:
            event_rows.append(_event_row(ev, state, pa))

        max_offset = int(cfg.confirmation_window_candles)
        for offset in range(0, max_offset + 1):
            idx = i0 + offset
            if idx >= len(frame):
                break
            row = frame.iloc[idx]
            atr_raw = atr_series.iloc[idx]
            atr_val = None
            try:
                if atr_raw == atr_raw and float(atr_raw) > 0:
                    atr_val = float(atr_raw)
            except (TypeError, ValueError):
                atr_val = None
            closed = {
                "timestamp": row["timestamp"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]) if "volume" in row and row["volume"] == row["volume"] else None,
                "candle_index": int(idx),
                "atr": atr_val,
            }
            # Opposing setup: any opposite-side activation whose decision_time equals
            # this candle's timestamp + 5m is approximated by matching activation ts
            # to decision_time (== closed candle open + 5m in pipeline). Also match
            # exact candle timestamp for robustness.
            opposing = None
            candle_key = _ts_str(row["timestamp"])
            # decision_time is typically candle_ts + 5m
            decision_key = _ts_str(pd.Timestamp(row["timestamp"]) + pd.Timedelta(minutes=5))
            candidates = setups_by_ts.get(decision_key, []) + setups_by_ts.get(candle_key, [])
            for s in candidates:
                if s.get("setup_side") in {"long", "short"} and s.get("setup_side") != pa.get("side"):
                    opposing = {
                        "setup_activated": True,
                        "setup_side": s.get("setup_side"),
                    }
                    break

            before_n = len(state.get("event_log") or [])
            state = update_momentum_state(
                state,
                closed,
                atr=atr_val,
                rolling_median_volume=_rolling_median_volume(volumes, idx, volume_median_window),
                opposing_setup=opposing,
            )
            for ev in (state.get("event_log") or [])[before_n:]:
                event_rows.append(_event_row(ev, state, pa))
            if state.get("candle_diagnostics"):
                diagnostic_rows.append(dict(state["candle_diagnostics"][-1]))

            if state["state"] in {"momentum_confirmed", "invalidated", "rejected", "expired"}:
                break

        conf = evaluate_momentum_confirmation(state)
        if conf is not None:
            row_out = {
                **conf,
                "setup_id": pa.get("setup_id"),
                "pa_setup_activation_timestamp": pa.get("setup_activation_timestamp"),
                "pa_structure_break_timestamp": pa.get("structure_break_timestamp"),
                "pa_pattern_type": pa.get("pattern_type"),
                "pa_warnings": list(pa.get("warnings") or []),
                "final_state": state.get("state"),
            }
            confirmation_rows.append(row_out)
            ctype = conf.get("confirmation_type")
            if ctype == "break_candle" and detail_cases["confirmed_on_break_candle"] is None:
                detail_cases["confirmed_on_break_candle"] = row_out
            if ctype and ctype != "break_candle" and detail_cases["confirmed_later"] is None:
                detail_cases["confirmed_later"] = row_out
        else:
            if detail_cases["not_confirmed"] is None:
                detail_cases["not_confirmed"] = {
                    "setup_id": pa.get("setup_id"),
                    "side": pa.get("side"),
                    "pattern_type": pa.get("pattern_type"),
                    "final_state": state.get("state"),
                    "invalidation_reason": state.get("invalidation_reason"),
                    "latest_failed": (state.get("latest_condition_result") or {}).get("failed"),
                    "diagnostics": state.get("candle_diagnostics"),
                }

    summary = build_momentum_summary(
        price_action_confirmations=price_action_confirmations,
        momentum_confirmations=confirmation_rows,
        event_rows=event_rows,
        diagnostic_rows=diagnostic_rows,
        detail_cases=detail_cases,
        momentum_config=cfg,
    )
    return {
        "summary": summary,
        "momentum_events": event_rows,
        "momentum_confirmations": confirmation_rows,
        "momentum_diagnostics": diagnostic_rows,
        "detail_cases": detail_cases,
    }


def _event_row(
    event: dict[str, Any],
    state: dict[str, Any],
    pa: dict[str, Any],
) -> dict[str, Any]:
    return {
        "setup_id": state.get("setup_id") or pa.get("setup_id"),
        "side": state.get("side") or pa.get("side"),
        "pattern_type": state.get("pattern_type") or pa.get("pattern_type"),
        "event": event.get("event"),
        "timestamp": event.get("timestamp"),
        "state": event.get("state") or state.get("state"),
        "reason": event.get("reason"),
        "candle_offset": event.get("candle_offset"),
        "confirmation_type": event.get("confirmation_type"),
        "confidence": event.get("confidence"),
        "passed_conditions": event.get("passed_conditions"),
        "failed_conditions": event.get("failed_conditions"),
    }


def build_momentum_summary(
    *,
    price_action_confirmations: list[dict[str, Any]],
    momentum_confirmations: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
    diagnostic_rows: list[dict[str, Any]],
    detail_cases: dict[str, Any],
    momentum_config: MomentumConfig,
) -> dict[str, Any]:
    pa_n = len(price_action_confirmations)
    mom_n = len(momentum_confirmations)
    by_side = Counter(c.get("side") for c in momentum_confirmations)
    by_pattern = Counter(c.get("pattern_type") for c in momentum_confirmations)
    by_type = Counter(c.get("confirmation_type") for c in momentum_confirmations)
    by_offset = Counter(c.get("candles_after_price_action_confirmation") for c in momentum_confirmations)
    confidence = Counter(c.get("confidence") for c in momentum_confirmations)

    terminal_events = Counter(
        e.get("event")
        for e in event_rows
        if e.get("event") in {"invalidated", "rejected", "expired", "momentum_confirmed"}
    )

    failed_counter: Counter[str] = Counter()
    for d in diagnostic_rows:
        for f in d.get("failed_conditions") or []:
            failed_counter[str(f)] += 1

    # HTF share among confirmed vs not
    pa_by_id = {str(p.get("setup_id")): p for p in price_action_confirmations}
    confirmed_ids = {str(c.get("setup_id")) for c in momentum_confirmations}
    htf_confirmed = 0
    htf_not = 0
    for sid, pa in pa_by_id.items():
        htf = "HTF_TRANSITION" in (pa.get("warnings") or [])
        if sid in confirmed_ids:
            if htf:
                htf_confirmed += 1
        else:
            if htf:
                htf_not += 1

    return {
        "price_action_confirmations": pa_n,
        "momentum_confirmations": mom_n,
        "momentum_quote": (float(mom_n / pa_n) if pa_n else None),
        "momentum_by_side": dict(by_side),
        "momentum_by_pattern": dict(by_pattern),
        "confirmation_type_counts": dict(by_type),
        "confirmation_offset_counts": {str(k): v for k, v in by_offset.items()},
        "break_candle_confirmations": int(by_type.get("break_candle", 0)),
        "candle_1_confirmations": int(by_offset.get(1, 0)),
        "candle_2_confirmations": int(by_offset.get(2, 0)),
        "candle_3_confirmations": int(by_offset.get(3, 0)),
        "invalidated": int(terminal_events.get("invalidated", 0)),
        "rejected": int(terminal_events.get("rejected", 0)),
        "expired": int(terminal_events.get("expired", 0)),
        "confidence_distribution": dict(confidence),
        "most_common_failed_conditions": failed_counter.most_common(10),
        "htf_transition_among_momentum_confirmed": htf_confirmed,
        "htf_transition_among_not_confirmed": htf_not,
        "momentum_config": momentum_config.to_dict(),
        "window_note": (
            "Break candle = age 0; then ages 1..confirmation_window_candles; "
            "expire after unsuccessful evaluation of the last window candle"
        ),
        "detail_cases": detail_cases,
    }


def format_momentum_summary_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Momentum Audit (Phase 3)",
        "",
        f"- PA confirmations: **{summary.get('price_action_confirmations')}**",
        f"- Momentum confirmations: **{summary.get('momentum_confirmations')}**",
        f"- Quote PA→Momentum: **{summary.get('momentum_quote')}**",
        f"- By side: `{summary.get('momentum_by_side')}`",
        f"- By pattern: `{summary.get('momentum_by_pattern')}`",
        f"- Break-candle: **{summary.get('break_candle_confirmations')}**",
        f"- Candle 1/2/3: "
        f"**{summary.get('candle_1_confirmations')}** / "
        f"**{summary.get('candle_2_confirmations')}** / "
        f"**{summary.get('candle_3_confirmations')}**",
        f"- invalidated / rejected / expired: "
        f"**{summary.get('invalidated')}** / "
        f"**{summary.get('rejected')}** / "
        f"**{summary.get('expired')}**",
        f"- Confidence: `{summary.get('confidence_distribution')}`",
        f"- Top failed conditions: `{summary.get('most_common_failed_conditions')}`",
        f"- HTF among confirmed / not: "
        f"**{summary.get('htf_transition_among_momentum_confirmed')}** / "
        f"**{summary.get('htf_transition_among_not_confirmed')}**",
        "",
        f"- Window: `{summary.get('window_note')}`",
        "",
        "## Detail cases",
        "",
        "```json",
        json.dumps(json_safe(summary.get("detail_cases")), indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def write_momentum_audit_outputs(
    payload: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = payload.get("summary") or {}
    paths = {
        "summary_json": out / "momentum_summary.json",
        "summary_md": out / "momentum_summary.md",
        "events_csv": out / "momentum_events.csv",
        "events_json": out / "momentum_events.json",
        "confirmations_csv": out / "momentum_confirmations.csv",
        "confirmations_json": out / "momentum_confirmations.json",
        "diagnostics_csv": out / "momentum_diagnostics.csv",
        "diagnostics_json": out / "momentum_diagnostics.json",
    }
    paths["summary_json"].write_text(
        json.dumps(json_safe(summary), indent=2, allow_nan=False),
        encoding="utf-8",
    )
    paths["summary_md"].write_text(format_momentum_summary_md(summary), encoding="utf-8")

    def _write(key_csv: str, key_json: str, rows: list[dict[str, Any]]) -> None:
        safe = json_safe(rows)
        paths[key_json].write_text(json.dumps(safe, indent=2, allow_nan=False), encoding="utf-8")
        flat = []
        for row in rows:
            item = dict(row)
            for field in ("passed_conditions", "failed_conditions", "pa_warnings", "reason_codes"):
                if isinstance(item.get(field), list):
                    item[field] = json.dumps(item[field], ensure_ascii=True)
            flat.append(item)
        pd.DataFrame(flat).to_csv(paths[key_csv], index=False)

    _write("events_csv", "events_json", payload.get("momentum_events") or [])
    _write("confirmations_csv", "confirmations_json", payload.get("momentum_confirmations") or [])
    _write("diagnostics_csv", "diagnostics_json", payload.get("momentum_diagnostics") or [])
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Momentum audit over existing PA confirmations (research-only)."
    )
    parser.add_argument(
        "--pipeline-dir",
        required=True,
        help="Directory with price_action_confirmations.json and candles from pipeline",
    )
    parser.add_argument(
        "--candles-csv",
        default=None,
        help="Optional candles CSV; otherwise reload via pipeline audit helpers",
    )
    parser.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_momentum_audit",
    )
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--start", default="2026-03-01")
    parser.add_argument("--end", default="2026-03-08")
    return parser


def main(argv: list[str] | None = None) -> int:
    from .data_loader import load_symbol_candles
    from .signal_tp_audit import prepare_candle_window

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    pipeline_dir = Path(args.pipeline_dir)
    pa_confs = json.loads((pipeline_dir / "price_action_confirmations.json").read_text())
    setups_path = pipeline_dir / "setup_activations.json"
    setups = json.loads(setups_path.read_text()) if setups_path.exists() else []

    if args.candles_csv:
        frame = pd.read_csv(args.candles_csv)
    else:
        raw = load_symbol_candles(args.symbol)
        prepared = prepare_candle_window(
            raw,
            start=args.start,
            end=args.end,
            history_candles=144,
            timeframes="5m,15m,30m",
        )
        frame = prepared["candles"]

    payload = run_momentum_audit(
        price_action_confirmations=pa_confs,
        candles=frame,
        setup_activations=setups,
    )
    paths = write_momentum_audit_outputs(payload, args.output_dir)
    summary = payload["summary"]
    print(
        f"Momentum audit: PA={summary.get('price_action_confirmations')} "
        f"momentum={summary.get('momentum_confirmations')} "
        f"quote={summary.get('momentum_quote')}"
    )
    for path in paths.values():
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
