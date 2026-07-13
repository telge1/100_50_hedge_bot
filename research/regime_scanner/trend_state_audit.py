"""APTUSDT trend-state audit (research-only, counterfactual).

Window defaults: 2026-03-05 18:00 UTC → 2026-03-10 00:00 UTC.
Does not hardcode expected transition clock times as trading rules.
Does not mutate existing pipeline CSVs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_state_machine import (
    default_trend_state_config,
    run_trend_state_timeline,
)
from research.regime_scanner.trend_state_policy import would_block_long, would_block_short

DEFAULT_AUDIT_START = "2026-03-05T18:00:00+00:00"
DEFAULT_AUDIT_END = "2026-03-10T00:00:00+00:00"
# Warmup bars before audit start (spec ≥220 5m); load extra calendar days.
DEFAULT_WARM_PAD_DAYS = 3

DEFAULT_PIPELINE = (
    "research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum"
)
DEFAULT_OUT = "research/backtests/results/regime_scanner_trend_state_audit_march_0608"


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def prepare_indicator_frame(
    symbol: str,
    *,
    audit_start: object,
    audit_end: object,
    warm_pad_days: int = DEFAULT_WARM_PAD_DAYS,
) -> pd.DataFrame:
    start = _ts(audit_start)
    end = _ts(audit_end)
    warm_start = start - pd.Timedelta(days=int(warm_pad_days))
    raw = load_symbol_candles(symbol)
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    slice_ = raw[(raw["timestamp"] >= warm_start) & (raw["timestamp"] < end)].copy()
    cfg = default_regime_scanner_config().with_timeframe("5m")
    frame = compute_indicator_frame(slice_, config=cfg)
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)
    return frame


def snapshots_to_frame(snapshots: list[Any]) -> pd.DataFrame:
    rows = [s.to_dict() if hasattr(s, "to_dict") else dict(s) for s in snapshots]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "decision_time" in df.columns:
        df["decision_time"] = pd.to_datetime(df["decision_time"], utc=True)
    return df


def extract_transitions(timeline: pd.DataFrame) -> pd.DataFrame:
    if timeline.empty:
        return pd.DataFrame()
    df = timeline.sort_values("decision_time").reset_index(drop=True)
    prev = df["current_state"].shift(1)
    changed = df["current_state"] != prev
    changed.iloc[0] = True
    out = df.loc[changed].copy()
    out["from_state"] = prev.loc[changed].values
    out["to_state"] = out["current_state"]
    cols = [
        "decision_time",
        "from_state",
        "to_state",
        "active_reasons",
        "state_confidence",
        "structure_5m",
        "structure_15m",
        "context_30m",
        "allow_long",
        "allow_short",
    ]
    return out[[c for c in cols if c in out.columns]]


def extract_events(all_events: list[Any], *, start: object, end: object) -> pd.DataFrame:
    start_ts = _ts(start)
    end_ts = _ts(end)
    rows = []
    for ev in all_events:
        payload = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
        et = _ts(payload["event_time"])
        if et < start_ts or et > end_ts:
            continue
        rows.append(payload)
    return pd.DataFrame(rows)


def load_pipeline_events(pipeline_dir: Path, *, start: object, end: object) -> dict[str, pd.DataFrame]:
    start_ts = _ts(start)
    end_ts = _ts(end)
    out: dict[str, pd.DataFrame] = {}
    mapping = {
        "setups": ("setup_activations.csv", "setup_activation_timestamp"),
        "pa": ("price_action_confirmations.csv", "structure_break_timestamp"),
        "mom": ("momentum_confirmations.csv", "confirmation_timestamp"),
    }
    for key, (name, ts_col) in mapping.items():
        path = pipeline_dir / name
        if not path.exists():
            out[key] = pd.DataFrame()
            continue
        df = pd.read_csv(path)
        if ts_col not in df.columns:
            out[key] = df
            continue
        df[ts_col] = pd.to_datetime(df[ts_col], utc=True)
        out[key] = df[(df[ts_col] >= start_ts) & (df[ts_col] <= end_ts)].copy()
    return out


def join_counterfactual(
    timeline: pd.DataFrame,
    pipeline: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Read-only join: would policy have blocked each pipeline event?"""
    if timeline.empty:
        return pd.DataFrame()
    tl = timeline.sort_values("decision_time")[
        ["decision_time", "current_state", "allow_long", "allow_short", "policy"]
    ].copy()
    rows: list[dict[str, Any]] = []
    for kind, ts_col, side_col in (
        ("setup", "setup_activation_timestamp", "setup_side"),
        ("pa", "structure_break_timestamp", "side"),
        ("mom", "confirmation_timestamp", "side"),
    ):
        key = {"setup": "setups", "pa": "pa", "mom": "mom"}[kind]
        df = pipeline.get(key)
        if df is None or df.empty or ts_col not in df.columns:
            continue
        for _, r in df.iterrows():
            ts = _ts(r[ts_col])
            side = str(r.get(side_col) or r.get("side") or "").lower()
            prior = tl[tl["decision_time"] <= ts]
            if prior.empty:
                state = "unavailable"
                allow_long = True
                allow_short = True
            else:
                last = prior.iloc[-1]
                state = str(last["current_state"])
                allow_long = bool(last["allow_long"])
                allow_short = bool(last["allow_short"])
            blocked = False
            if side == "long":
                blocked = (not allow_long) or would_block_long(state)
            elif side == "short":
                blocked = (not allow_short) or would_block_short(state)
            rows.append(
                {
                    "event_kind": kind,
                    "event_time": ts.isoformat(),
                    "side": side,
                    "trend_state": state,
                    "allow_long": allow_long,
                    "allow_short": allow_short,
                    "counterfactual_blocked": blocked,
                    "setup_id": r.get("setup_id"),
                }
            )
    return pd.DataFrame(rows)


def summarize_audit(
    timeline: pd.DataFrame,
    transitions: pd.DataFrame,
    events: pd.DataFrame,
    counterfactual: pd.DataFrame,
) -> dict[str, Any]:
    state_counts: dict[str, int] = {}
    if not timeline.empty:
        state_counts = timeline["current_state"].value_counts().to_dict()
    first_seen: dict[str, str | None] = {}
    for state in (
        "bearish_warning",
        "early_bearish",
        "strong_bearish",
        "bearish_weakening",
        "bottoming",
        "bullish_warning",
        "early_bullish",
        "strong_bullish",
        "bullish_weakening",
        "topping",
    ):
        hit = timeline[timeline["current_state"] == state] if not timeline.empty else timeline
        first_seen[state] = (
            None if hit.empty else str(hit.iloc[0]["decision_time"])
        )

    long_block_start = None
    short_block_start = None
    if not timeline.empty:
        lb = timeline[timeline["allow_long"] == False]
        sb = timeline[timeline["allow_short"] == False]
        if not lb.empty:
            long_block_start = str(lb.iloc[0]["decision_time"])
        if not sb.empty:
            short_block_start = str(sb.iloc[0]["decision_time"])

    flutter = 0
    if not timeline.empty and "min_hold_remaining" in timeline.columns:
        # Heuristic: transition while previous min_hold would still apply
        # (age was reset — count rapid same-day flips)
        if len(transitions) >= 2:
            ttimes = pd.to_datetime(transitions["decision_time"], utc=True)
            deltas = ttimes.diff().dt.total_seconds().fillna(1e9) / 60.0
            flutter = int((deltas < 15).sum())  # <3 bars between flips

    choch_times = []
    choch_5m_times = []
    if not events.empty and "event_type" in events.columns:
        choch = events[events["event_type"].isin(["bullish_choch", "bearish_choch"])]
        choch_times = [str(x) for x in choch["event_time"].tolist()]
        if "timeframe" in choch.columns:
            choch5 = choch[choch["timeframe"] == "5m"]
            choch_5m_times = [
                f"{r.event_type}@{r.event_time}" for r in choch5.itertuples()
            ]
        else:
            choch_5m_times = choch_times

    cf_blocks = {}
    if not counterfactual.empty:
        cf_blocks = {
            "total_events": int(len(counterfactual)),
            "blocked": int(counterfactual["counterfactual_blocked"].sum()),
            "blocked_long": int(
                counterfactual[
                    (counterfactual["side"] == "long")
                    & (counterfactual["counterfactual_blocked"])
                ].shape[0]
            ),
            "blocked_short": int(
                counterfactual[
                    (counterfactual["side"] == "short")
                    & (counterfactual["counterfactual_blocked"])
                ].shape[0]
            ),
        }

    return {
        "state_time_shares_bars": state_counts,
        "first_seen_decision_time": first_seen,
        "n_transitions": int(len(transitions)),
        "flutter_heuristic_fast_flips": flutter,
        "long_block_start": long_block_start,
        "short_block_start": short_block_start,
        "choch_event_times": choch_times,
        "n_structure_events_in_window": int(len(events)),
        "counterfactual": cf_blocks,
        "answers": {
            "1_bearish_warning": first_seen.get("bearish_warning"),
            "2_early_bearish": first_seen.get("early_bearish"),
            "3_strong_bearish": first_seen.get("strong_bearish"),
            "4_bearish_weakening": first_seen.get("bearish_weakening"),
            "5_bottoming": first_seen.get("bottoming"),
            "6_bullish_choch_events": choch_times,
            "6b_choch_5m_typed": choch_5m_times[:40],
            "7_early_bullish": first_seen.get("early_bullish"),
            "8_strong_bullish": first_seen.get("strong_bullish"),
            "9_longs_blocked_from": long_block_start,
            "10_shorts_blocked_from": short_block_start,
            "11_n_state_changes": int(len(transitions)),
            "12_flutter_heuristic": flutter,
        },
        "note": (
            "Timestamps are causal outputs of the research machine, not hardcoded "
            "expectations. Thresholds were not fitted to this window."
        ),
    }


def write_markdown_report(summary: dict[str, Any], out_dir: Path) -> Path:
    ans = summary.get("answers") or {}
    lines = [
        "# Trend State Audit (APTUSDT)",
        "",
        "Research-only counterfactual. No pipeline mutation.",
        "",
        "## First-seen states (causal)",
        "",
    ]
    for k, v in ans.items():
        lines.append(f"- **{k}**: `{v}`")
    lines.extend(
        [
            "",
            "## State bar counts",
            "",
            "```json",
            json.dumps(summary.get("state_time_shares_bars") or {}, indent=2),
            "```",
            "",
            "## Counterfactual pipeline join",
            "",
            "```json",
            json.dumps(summary.get("counterfactual") or {}, indent=2),
            "```",
            "",
            summary.get("note") or "",
            "",
        ]
    )
    path = out_dir / "trend_state_audit_summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_trend_state_audit(
    *,
    symbol: str = "APTUSDT",
    audit_start: object = DEFAULT_AUDIT_START,
    audit_end: object = DEFAULT_AUDIT_END,
    pipeline_dir: str | Path = DEFAULT_PIPELINE,
    out_dir: str | Path = DEFAULT_OUT,
    warm_pad_days: int = DEFAULT_WARM_PAD_DAYS,
) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    frame = prepare_indicator_frame(
        symbol,
        audit_start=audit_start,
        audit_end=audit_end,
        warm_pad_days=warm_pad_days,
    )
    cfg = default_trend_state_config()
    snapshots, _rt, all_events = run_trend_state_timeline(
        frame,
        cfg=cfg,
        start_decision_time=audit_start,
        end_decision_time=audit_end,
    )
    timeline = snapshots_to_frame(snapshots)
    transitions = extract_transitions(timeline)
    events = extract_events(all_events, start=audit_start, end=audit_end)
    pipeline = load_pipeline_events(Path(pipeline_dir), start=audit_start, end=audit_end)
    counterfactual = join_counterfactual(timeline, pipeline)
    summary = summarize_audit(timeline, transitions, events, counterfactual)
    summary["symbol"] = symbol
    summary["audit_start"] = str(_ts(audit_start))
    summary["audit_end"] = str(_ts(audit_end))
    summary["config"] = cfg.to_dict()

    timeline.to_csv(out / "trend_state_timeline.csv", index=False)
    transitions.to_csv(out / "trend_state_transitions.csv", index=False)
    if not events.empty:
        events.to_csv(out / "trend_structure_events.csv", index=False)
    if not counterfactual.empty:
        counterfactual.to_csv(out / "trend_state_counterfactual.csv", index=False)
    (out / "trend_state_audit_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2),
        encoding="utf-8",
    )
    write_markdown_report(summary, out)
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--audit-start", default=DEFAULT_AUDIT_START)
    p.add_argument("--audit-end", default=DEFAULT_AUDIT_END)
    p.add_argument("--pipeline-dir", default=DEFAULT_PIPELINE)
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument("--warm-pad-days", type=int, default=DEFAULT_WARM_PAD_DAYS)
    args = p.parse_args(argv)
    summary = run_trend_state_audit(
        symbol=args.symbol,
        audit_start=args.audit_start,
        audit_end=args.audit_end,
        pipeline_dir=args.pipeline_dir,
        out_dir=args.out_dir,
        warm_pad_days=args.warm_pad_days,
    )
    print(json.dumps(json_safe(summary.get("answers")), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
