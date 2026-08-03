#!/usr/bin/env python3
"""Read-only printer: structure-break timestamps for the five problem regimes.

Reads only existing artefacts under results/c3_frozen_warning_five_regime_timeline/.
Does not run the scanner, Frozen V1, backtests, or recompute structure breaks.
Does not write any files.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "results" / "c3_frozen_warning_five_regime_timeline"
EXPECTED = [
    "BLK_APTUSDT_001",
    "BLK_APTUSDT_002",
    "BLK_APTUSDT_003",
    "BLK_DOGEUSDT_001",
    "BLK_DOGEUSDT_002",
]
TZ_TANZANIA = timezone(timedelta(hours=3))
TF = "5m"
CANONICAL = "transition_candidates.csv (candidate=T_FIRST_STRUCTURE_BREAK)"


def _parse_utc(val: Any) -> datetime | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() in {"nan", "none", "nat"}:
        return None
    ts = pd.Timestamp(s)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.to_pydatetime()


def _fmt(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_tz(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(TZ_TANZANIA).strftime("%Y-%m-%d %H:%M:%S EAT (UTC+3)")


def _iso_z(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes(a: datetime | None, b: datetime | None) -> float | None:
    """Minutes from a → b (positive if a before b)."""
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60.0


def _rel_label(early: datetime | None, break_ts: datetime | None) -> str:
    if early is None or break_ts is None:
        return "n/a"
    mins = _minutes(early, break_ts)
    assert mins is not None
    if mins > 0:
        return f"EARLY {mins:.0f}m before break"
    if mins < 0:
        return f"EARLY {-mins:.0f}m after break"
    return "EARLY at break"


def load_records(audit_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load five structure-break records from existing CSVs only."""
    notes: list[str] = []
    tc = pd.read_csv(audit_dir / "transition_candidates.csv")
    sb = pd.read_csv(audit_dir / "structure_break_comparison.csv")
    inv = pd.read_csv(audit_dir / "five_regime_inventory.csv")
    cases = pd.read_csv(audit_dir / "regime_case_decisions.csv")

    break_tc = tc[tc["candidate"] == "T_FIRST_STRUCTURE_BREAK"].copy()
    if sorted(break_tc["cluster_id"].tolist()) != EXPECTED:
        notes.append(
            f"WARNING: transition_candidates cluster set != expected five: "
            f"{sorted(break_tc['cluster_id'].tolist())}"
        )

    # Cross-file consistency (no guessing which wins — report all diffs)
    for cid in EXPECTED:
        t_tc = _parse_utc(break_tc.loc[break_tc.cluster_id == cid, "timestamp"].iloc[0])
        t_sb = _parse_utc(sb.loc[sb.cluster_id == cid, "T_FIRST_STRUCTURE_BREAK"].iloc[0])
        t_inv = _parse_utc(inv.loc[inv.cluster_id == cid, "structure_break"].iloc[0])
        if not (t_tc == t_sb == t_inv):
            notes.append(
                f"DEVIATION {cid}: transition_candidates={_iso_z(t_tc)} "
                f"structure_break_comparison={_iso_z(t_sb)} "
                f"five_regime_inventory={_iso_z(t_inv)} "
                f"— canonical = {CANONICAL}"
            )

    # Timeline: market OHLC at row timestamp = candle open of that row.
    # Structure-break event is marked on the row whose timestamp == available_at
    # (causal known). Break candle itself opened 5m earlier.
    tl_cols = [
        "timestamp",
        "cluster_id",
        "symbol",
        "market_open",
        "market_high",
        "market_low",
        "market_close",
        "trend_direction",
        "trend_state",
        "protected_high",
        "protected_low",
        "external_bos",
        "choch",
        "warning_state",
        "structure_break_event",
        "trend_structure_break_ts",
    ]
    tl = pd.read_csv(audit_dir / "regime_timeline.csv", usecols=tl_cols)
    tl["timestamp"] = pd.to_datetime(tl["timestamp"], utc=True)

    records: list[dict[str, Any]] = []
    for cid in EXPECTED:
        row_tc = break_tc.loc[break_tc.cluster_id == cid].iloc[0]
        row_sb = sb.loc[sb.cluster_id == cid].iloc[0]
        row_case = cases.loc[cases.cluster_id == cid].iloc[0]
        symbol = str(row_tc["symbol"])
        known = _parse_utc(row_tc["timestamp"])
        assert known is not None
        candle_open = known - timedelta(minutes=5)
        candle_close = known  # available_at == candle close for 5m [open, close)

        g = tl[tl["cluster_id"] == cid].sort_values("timestamp")
        mark = g[g["timestamp"] == pd.Timestamp(known)]
        brk = g[g["timestamp"] == pd.Timestamp(candle_open)]
        prev = g[g["timestamp"] == pd.Timestamp(candle_open - timedelta(minutes=5))]

        if mark.empty:
            notes.append(f"MISSING timeline mark row for {cid} at {_iso_z(known)}")
        if brk.empty:
            notes.append(f"MISSING timeline break-candle-open row for {cid} at {_iso_z(candle_open)}")

        # Event-marker row must match canonical ts
        if not mark.empty:
            mts = mark.iloc[0].get("trend_structure_break_ts")
            if pd.notna(mts) and _parse_utc(mts) != known:
                notes.append(
                    f"DEVIATION {cid}: regime_timeline.trend_structure_break_ts="
                    f"{mts} vs canonical {_iso_z(known)}"
                )
            if int(mark.iloc[0].get("structure_break_event") or 0) != 1:
                notes.append(f"WARNING {cid}: structure_break_event!=1 on mark row")

        mark_r = mark.iloc[0] if not mark.empty else None
        brk_r = brk.iloc[0] if not brk.empty else None
        prev_r = prev.iloc[0] if not prev.empty else None

        early = _parse_utc(row_sb.get("T_EARLY"))
        pers = _parse_utc(row_sb.get("T_PERSISTENT"))
        high = _parse_utc(row_sb.get("T_HIGH_RISK"))

        # Break type from fields present on the causal mark row (no new scanner logic)
        parts: list[str] = []
        if mark_r is not None:
            if str(mark_r.get("choch") or "") in {"down", "bearish"}:
                parts.append("CHOCH_down")
            if str(mark_r.get("external_bos") or "") == "down":
                parts.append("external_BOS_down")
            ts_state = str(mark_r.get("trend_state") or "")
            if ts_state:
                parts.append(f"trend_state={ts_state}")
        break_type = " + ".join(parts) if parts else "unknown_from_timeline_fields"

        level_price = None
        level_type = ""
        if mark_r is not None and pd.notna(mark_r.get("protected_low")):
            level_type = "protected_low"
            level_price = float(mark_r["protected_low"])
        elif mark_r is not None and pd.notna(mark_r.get("protected_high")):
            level_type = "protected_high"
            level_price = float(mark_r["protected_high"])
        else:
            level_type = "not_in_timeline_artefacts"
            notes.append(
                f"GAP {cid}: protected_high/low not both present on mark row "
                f"(high={None if mark_r is None else mark_r.get('protected_high')}, "
                f"low={None if mark_r is None else mark_r.get('protected_low')})"
            )

        # OHLC of break candle = timeline row at candle_open (not the mark row)
        notes.append(
            f"SEMANTICS {cid}: canonical T_FIRST_STRUCTURE_BREAK={_iso_z(known)} is "
            f"available_at / causal known (= candle close). Break candle open="
            f"{_iso_z(candle_open)}. Timeline mark-row market_* at known ts is the "
            f"NEXT candle after the break candle — OHLC below uses open-row."
        )

        rec = {
            "cluster_id": cid,
            "symbol": symbol,
            "case_decision": str(row_case.get("case_decision") or ""),
            "structure_break_ts_utc": _iso_z(known),
            "structure_break_ts_tanzania": _fmt_tz(known),
            "structure_break_candle_open_utc": _iso_z(candle_open),
            "structure_break_candle_close_utc": _iso_z(candle_close),
            "timeframe": TF,
            "timestamp_semantics": (
                "canonical ts = available_at = candle close / first moment scanner "
                "state with adverse structure is causally known; candle open = ts−5m"
            ),
            "trend_direction_before_break": (
                None if brk_r is None else brk_r.get("trend_direction")
            ),
            "break_type": break_type,
            "structure_level_type": level_type,
            "structure_level_price": level_price,
            "break_candle_open": None if brk_r is None else float(brk_r["market_open"]),
            "break_candle_high": None if brk_r is None else float(brk_r["market_high"]),
            "break_candle_low": None if brk_r is None else float(brk_r["market_low"]),
            "break_candle_close": None if brk_r is None else float(brk_r["market_close"]),
            "previous_candle_close": (
                None if prev_r is None else float(prev_r["market_close"])
            ),
            "scanner_state_before": None if brk_r is None else brk_r.get("trend_state"),
            "scanner_state_at_break": None if mark_r is None else mark_r.get("trend_state"),
            "external_bos": None if mark_r is None else mark_r.get("external_bos"),
            "choch": None if mark_r is None else mark_r.get("choch"),
            "protected_high": (
                None
                if mark_r is None or pd.isna(mark_r.get("protected_high"))
                else float(mark_r["protected_high"])
            ),
            "protected_low": (
                None
                if mark_r is None or pd.isna(mark_r.get("protected_low"))
                else float(mark_r["protected_low"])
            ),
            "warning_state_at_break": None if mark_r is None else mark_r.get("warning_state"),
            "first_early_ts": _iso_z(early),
            "first_persistent_ts": _iso_z(pers),
            "first_high_risk_ts": _iso_z(high),
            "minutes_early_to_break": _minutes(early, known),
            "minutes_persistent_to_break": _minutes(pers, known),
            "minutes_high_to_break": _minutes(high, known),
            "early_relative": _rel_label(early, known),
            "persistent_relative": _rel_label(pers, known).replace("EARLY", "PERSISTENT"),
            "chart_window_start_utc": _iso_z(known - timedelta(minutes=60)),
            "chart_window_end_utc": _iso_z(known + timedelta(minutes=60)),
            "chart_window_start_tanzania": _fmt_tz(known - timedelta(minutes=60)),
            "chart_window_end_tanzania": _fmt_tz(known + timedelta(minutes=60)),
            "chart_window_6h_start_utc": _iso_z(known - timedelta(hours=6)),
            "chart_window_6h_end_utc": _iso_z(known + timedelta(hours=6)),
            "chart_window_6h_start_tanzania": _fmt_tz(known - timedelta(hours=6)),
            "chart_window_6h_end_tanzania": _fmt_tz(known + timedelta(hours=6)),
            "canonical_source_file": CANONICAL,
        }
        records.append(rec)

    # Deduplicate SEMANTICS into a single global note + keep deviations/gaps
    sem = [n for n in notes if n.startswith("SEMANTICS ")]
    other = [n for n in notes if not n.startswith("SEMANTICS ")]
    if sem:
        other.insert(
            0,
            "SEMANTICS (all clusters): T_FIRST_STRUCTURE_BREAK is available_at "
            "(= candle close / causal known). Break candle open = known − 5m. "
            "regime_timeline mark-row market_* at known ts is the NEXT 5m candle; "
            "break OHLC is taken from the open-row (known − 5m).",
        )
    return records, other


def _attention(rec: dict[str, Any]) -> str:
    """Max 3 short sentences for TradingView focus."""
    level = rec["structure_level_type"]
    price = rec["structure_level_price"]
    s1 = (
        f"Scanner markiert am kausal bekannten Zeitpunkt einen adversen Bruch "
        f"({rec['break_type']}); Level aus Timeline: {level}={price}."
    )
    s2 = f"{rec['early_relative']}; {rec['persistent_relative']}."
    close = rec["break_candle_close"]
    low = rec["break_candle_low"]
    if price is not None and close is not None:
        if close < price:
            body = (
                f"Break-Candle schließt klar unter dem Level ({close} < {price}); "
                f"Low={low}. Prüfe Close vs Wick, Reclaim und Fortsetzung."
            )
        else:
            body = (
                f"Break-Candle close={close} vs Level={price} (Low={low}): "
                f"Close-vs-Wick, Reclaim und False Break prüfen."
            )
    else:
        body = "Im Chart: Close hinter Level vs Wick-only, Reclaim, Fortsetzung."
    return f"{s1} {s2} {body}"


def print_report(records: list[dict[str, Any]], notes: list[str]) -> None:
    print("=" * 100)
    print("C3 five-regime structure breaks (read-only from existing artefacts)")
    print(f"Canonical source: {CANONICAL}")
    print("Timestamp semantics: available_at = candle close = causally known")
    print("=" * 100)
    print()
    print(
        "| Symbol | Cluster | Break-Candle UTC | Kausal bekannt UTC | Tanzania-Zeit | "
        "Break-Typ | Level | EARLY relativ | PERSISTENT relativ |"
    )
    print(
        "| ------ | ------- | ---------------- | ------------------ | ------------- | "
        "--------- | ----: | ------------: | -----------------: |"
    )
    for r in records:
        level = (
            f"{r['structure_level_type']}={r['structure_level_price']}"
            if r["structure_level_price"] is not None
            else r["structure_level_type"]
        )
        tz_short = r["structure_break_ts_tanzania"].replace(" EAT (UTC+3)", " EAT")
        print(
            f"| {r['symbol']} | {r['cluster_id']} | "
            f"{r['structure_break_candle_open_utc']} | "
            f"{r['structure_break_ts_utc']} | {tz_short} | "
            f"{r['break_type']} | {level} | "
            f"{r['early_relative']} | {r['persistent_relative']} |"
        )

    print()
    print("--- Detailansicht ---")
    for r in records:
        print()
        print(f"### {r['cluster_id']} ({r['symbol']})  case={r['case_decision']}")
        keys = [
            "cluster_id",
            "symbol",
            "structure_break_ts_utc",
            "structure_break_ts_tanzania",
            "structure_break_candle_open_utc",
            "structure_break_candle_close_utc",
            "timeframe",
            "timestamp_semantics",
            "trend_direction_before_break",
            "break_type",
            "structure_level_type",
            "structure_level_price",
            "break_candle_open",
            "break_candle_high",
            "break_candle_low",
            "break_candle_close",
            "previous_candle_close",
            "scanner_state_before",
            "scanner_state_at_break",
            "external_bos",
            "choch",
            "protected_high",
            "protected_low",
            "warning_state_at_break",
            "first_early_ts",
            "first_persistent_ts",
            "first_high_risk_ts",
            "minutes_early_to_break",
            "minutes_persistent_to_break",
            "minutes_high_to_break",
            "chart_window_start_utc",
            "chart_window_end_utc",
            "chart_window_start_tanzania",
            "chart_window_end_tanzania",
            "chart_window_6h_start_utc",
            "chart_window_6h_end_utc",
            "chart_window_6h_start_tanzania",
            "chart_window_6h_end_tanzania",
            "canonical_source_file",
        ]
        for k in keys:
            print(f"  {k}: {r.get(k)}")
        print(f"  chart_attention: {_attention(r)}")

    print()
    print("--- Validierung / Hinweise ---")
    print(f"  n_clusters: {len(records)} (expected 5)")
    print(f"  unique_breaks: {len({r['structure_break_ts_utc'] for r in records})}")
    for n in notes:
        print(f"  - {n}")
    print()
    print("GAP: Boolean flags bearish_bos / close_break_protected_down are NOT stored")
    print("  in five-regime CSV artefacts; break_type uses external_bos/choch/trend_state")
    print("  from regime_timeline mark rows only (no scanner recompute).")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Print structure-break timestamps from five-regime timeline artefacts"
    )
    p.add_argument(
        "--audit-dir",
        type=Path,
        default=DEFAULT_DIR,
        help="Path to c3_frozen_warning_five_regime_timeline results",
    )
    p.add_argument("--symbol", default=None, help="Filter e.g. DOGEUSDT")
    p.add_argument("--cluster-id", default=None, help="Filter e.g. BLK_DOGEUSDT_002")
    args = p.parse_args(argv)

    if not args.audit_dir.is_dir():
        print(f"ERROR: audit dir missing: {args.audit_dir}", file=sys.stderr)
        return 1

    records, notes = load_records(args.audit_dir)
    if args.symbol:
        records = [r for r in records if r["symbol"] == args.symbol]
    if args.cluster_id:
        records = [r for r in records if r["cluster_id"] == args.cluster_id]
    if not records:
        print("ERROR: no clusters matched filters", file=sys.stderr)
        return 1

    print_report(records, notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
