"""Full-history cluster-mass + delta/OB TP calibration (Phases A–E).

Universe:
  - scanner long breakouts (legacy ∪ calibrated), entry from 5m open
  - Phase-A strong long breakouts (candle-labeled)
  - Phase-B long fakeouts (negative / dead-move cohort)

Window matches DOGE calibration coverage:
  2026-09-05T17:00Z → 2026-09-19T11:00Z
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_EXTRA = [
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src",
    "/home/telgenbuescher/projects/orderbook_analyse/src",
    str(_REPO),
    str(_REPO / "dashboard"),
]
for _p in reversed(_EXTRA):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import (
    DEFAULT_CLUSTER_GAP_PCT,
    analyze_signal_clusters,
    summarize_cross_signal,
)

SCAN_FROM = datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)
SCAN_TO = datetime(2026, 9, 19, 11, 0, tzinfo=timezone.utc)


@dataclass
class UniverseRow:
    source: str  # scanner_breakout | strong_breakout | fakeout
    decision_ts: datetime
    entry_price: float | None
    tier: str
    confirm_delta: float
    followthrough_delta: float
    side: str = "long"


def _parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_dotenv() -> None:
    env_path = Path(
        "/home/telgenbuescher/projects/Signal_Generator_Ralf/"
        "signal_generator_stoch_waves/.env"
    )
    if env_path.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path)
        except ImportError:
            pass


def _in_window(ts: datetime) -> bool:
    return SCAN_FROM <= ts <= SCAN_TO


def resolve_entry_from_bar(symbol: str, decision_ts: datetime) -> float | None:
    from ob_microstructure_breakout_bot.data.bars import load_5m_bars

    bars = load_5m_bars(
        symbol,
        decision_ts - timedelta(minutes=30),
        decision_ts + timedelta(minutes=10),
    )
    for b in bars:
        if b.ts == decision_ts:
            return float(b.open)
    prior = [b for b in bars if b.ts < decision_ts]
    if prior:
        return float(prior[-1].close)
    return None


def build_phase_a_universe(
    *,
    symbol: str,
    scanner_path: Path,
    strong_path: Path,
    fakeout_path: Path,
) -> list[UniverseRow]:
    """Phase A: all long candidates in the coverage window."""
    rows: list[UniverseRow] = []

    # 1) Scanner breakouts: union legacy + calibrated longs (calibrated wins)
    scanner = json.loads(scanner_path.read_text(encoding="utf-8"))
    by_key: dict[str, UniverseRow] = {}
    for bucket in ("legacy", "calibrated"):
        for ev in scanner.get(bucket, {}).get("breakouts", []):
            if ev.get("side") != "long":
                continue
            ts = _parse_ts(str(ev["decision_ts"]))
            if not _in_window(ts):
                continue
            key = ts.strftime("%Y-%m-%dT%H:%M")
            if key in by_key and bucket != "calibrated":
                continue
            by_key[key] = UniverseRow(
                source="scanner_breakout",
                decision_ts=ts,
                entry_price=None,
                tier=str(ev.get("tier") or bucket),
                confirm_delta=float(ev.get("confirm_delta") or 0.0),
                followthrough_delta=float(ev.get("followthrough_delta") or 0.0),
            )
    rows.extend(by_key.values())

    # 2) Phase-A strong candle breakouts (long)
    if strong_path.exists():
        strong = json.loads(strong_path.read_text(encoding="utf-8"))
        for ev in strong.get("events", []):
            if ev.get("direction") != "long":
                continue
            ts = _parse_ts(str(ev["decision_ts"]))
            if not _in_window(ts):
                continue
            rows.append(
                UniverseRow(
                    source="strong_breakout",
                    decision_ts=ts,
                    entry_price=float(ev.get("impulse_open") or 0.0) or None,
                    tier="phase_a_strong",
                    confirm_delta=float(ev.get("confirm_delta") or 0.0),
                    followthrough_delta=float(ev.get("followthrough_delta") or 0.0),
                )
            )

    # 3) Phase-B long fakeouts
    if fakeout_path.exists():
        fake = json.loads(fakeout_path.read_text(encoding="utf-8"))
        for ev in fake.get("events", []):
            if ev.get("direction") != "long":
                continue
            ts = _parse_ts(str(ev["decision_ts"]))
            if not _in_window(ts):
                continue
            rows.append(
                UniverseRow(
                    source="fakeout",
                    decision_ts=ts,
                    entry_price=float(ev.get("impulse_open") or 0.0) or None,
                    tier="phase_b_fakeout",
                    confirm_delta=float(ev.get("confirm_delta") or 0.0),
                    followthrough_delta=float(ev.get("followthrough_delta") or 0.0),
                )
            )

    rows.sort(key=lambda r: (r.decision_ts, r.source))
    # Resolve missing entries from bars
    for r in rows:
        if r.entry_price is None or r.entry_price <= 0:
            px = resolve_entry_from_bar(symbol, r.decision_ts)
            r.entry_price = px
    return [r for r in rows if r.entry_price is not None and r.entry_price > 0]


def derive_tp_hypotheses(summary: dict[str, Any], by_source: dict[str, Any]) -> list[str]:
    hyps: list[str] = []
    grid = summary.get("dist_delta_hit_grid") or []
    far_strong = next(
        (
            g
            for g in grid
            if g.get("dist_band") == "1.5-2.5%" and g.get("delta_bucket") == "delta_ge_200k"
        ),
        None,
    )
    near_any = [
        g for g in grid if g.get("dist_band") == "0.0-0.8%" and (g.get("n_clusters") or 0) > 0
    ]
    if near_any:
        hit = sum((g.get("hit_rate") or 0) * (g.get("n_clusters") or 0) for g in near_any)
        n = sum(g.get("n_clusters") or 0 for g in near_any)
        if n and hit / n >= 0.7:
            hyps.append(
                "Near clusters (<0.8%) are usually reachable → treat as noise / checkpoint, not primary TP."
            )
    if far_strong and (far_strong.get("hit_rate") or 0) < 0.4:
        hyps.append(
            "Far clusters (1.5–2.5%) stay hard to reach even with confirmΔ≥200k → require strong OB + high cluster mass."
        )

    outcomes = summary.get("by_outcome") or {}
    failed = outcomes.get("failed_early") or {}
    working = outcomes.get("working_to_strong_cluster") or {}
    if failed.get("n") and working.get("n"):
        if (failed.get("mean_entry_ob") or 99) < (working.get("mean_entry_ob") or 0):
            hyps.append(
                "Failed-early moves have weaker entry OB than working moves → OB is a TP-distance filter."
            )
        if (working.get("mean_reversal_strength_sum") or 0) > (
            failed.get("mean_reversal_strength_sum") or 0
        ):
            hyps.append(
                "Working reversals sit in heavier clusters → TP candidate = heaviest meaningful cluster, not nearest pool."
            )

    scanner = by_source.get("scanner_breakout") or {}
    if scanner.get("n_signals"):
        hyps.append(
            f"Scanner longs: strongest-by-mass hit rate "
            f"{(scanner.get('strongest_cluster_hit_rate') or 0):.0%} "
            f"(n={scanner.get('n_signals')})."
        )

    if not hyps:
        hyps.append("Need more samples; keep mass-first TP hypothesis provisional.")
    return hyps


def write_markdown_report(
    path: Path,
    *,
    payload: dict[str, Any],
    hypotheses: list[str],
) -> None:
    summary = payload.get("summary") or {}
    by_source = payload.get("by_source") or {}
    lines = [
        "# OB + Pool Cluster Full-History Calibration",
        "",
        f"Symbol: `{payload.get('symbol')}`",
        f"Window: `{payload.get('scan_from')}` → `{payload.get('scan_to')}`",
        f"Gap pct: `{payload.get('gap_pct')}`",
        "",
        "## Universe (Phase A)",
        "",
        f"- total rows analyzed: **{payload.get('n_analyzed')}**",
        f"- errors: **{payload.get('n_errors')}**",
        "",
        "| source | n | mean max% | strongest hit |",
        "|--------|---|-----------|---------------|",
    ]
    for src, sm in by_source.items():
        lines.append(
            f"| {src} | {sm.get('n_signals')} | "
            f"{(sm.get('mean_max_exc_pct') or 0):.2f} | "
            f"{(sm.get('strongest_cluster_hit_rate') or 0):.0%} |"
        )

    lines += [
        "",
        "## Outcomes (Phase D)",
        "",
        "```json",
        json.dumps(summary.get("outcome_counts") or {}, indent=2),
        "```",
        "",
        "### By outcome",
        "",
        "```json",
        json.dumps(summary.get("by_outcome") or {}, indent=2),
        "```",
        "",
        "## Dist × Delta reachability grid",
        "",
        "```json",
        json.dumps(summary.get("dist_delta_hit_grid") or [], indent=2),
        "```",
        "",
        "## Reversal cluster mass",
        "",
        f"- mean strength_sum: **{summary.get('mean_reversal_strength_sum')}**",
        f"- mean n_pools: **{summary.get('mean_reversal_n_pools')}**",
        f"- mean width_pct: **{summary.get('mean_reversal_width_pct')}**",
        "",
        "## Phase E — TP hypotheses",
        "",
    ]
    for h in hypotheses:
        lines.append(f"- {h}")
    lines += [
        "",
        "## Suggested rule draft (not live yet)",
        "",
        "1. Rank upper clusters by **strength_sum** (mass), not by nearest distance.",
        "2. Ignore / skip near micro-clusters as primary TP when entry OB is supportive.",
        "3. Only project TP to a far heavy cluster when confirmΔ is strong **and** entry OB is not flat/ask-heavy.",
        "4. Use the heaviest reached / reversal cluster as the calibration target for TP.",
        "",
        f"Raw JSON: `{payload.get('json_out')}`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Full-history cluster-mass TP calibration (Phases A–E)"
    )
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument(
        "--scanner",
        type=Path,
        default=_REPO
        / "ob_microstructure_breakout_bot"
        / "calibration"
        / "events"
        / "DOGEUSDT_backtest_legacy_vs_calibrated.json",
    )
    parser.add_argument(
        "--strong",
        type=Path,
        default=_REPO
        / "ob_microstructure_breakout_bot"
        / "calibration"
        / "events"
        / "DOGEUSDT_strong_breakouts_phase_a.json",
    )
    parser.add_argument(
        "--fakeouts",
        type=Path,
        default=_REPO
        / "ob_microstructure_breakout_bot"
        / "calibration"
        / "events"
        / "DOGEUSDT_fakeouts_phase_b.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_REPO / "results" / "ob_pool_cluster_full_history.json",
    )
    parser.add_argument(
        "--md-out",
        type=Path,
        default=_REPO / "results" / "ob_pool_cluster_full_history.md",
    )
    parser.add_argument("--gap-pct", type=float, default=DEFAULT_CLUSTER_GAP_PCT)
    parser.add_argument(
        "--sources",
        default="scanner_breakout,strong_breakout,fakeout",
        help="Comma list of sources to analyze",
    )
    args = parser.parse_args(argv)

    _load_dotenv()
    symbol = args.symbol.upper().replace("/", "")
    want = {s.strip() for s in str(args.sources).split(",") if s.strip()}

    print("Phase A: building universe ...", flush=True)
    universe = build_phase_a_universe(
        symbol=symbol,
        scanner_path=args.scanner,
        strong_path=args.strong,
        fakeout_path=args.fakeouts,
    )
    universe = [u for u in universe if u.source in want]
    print(
        f"  n={len(universe)} "
        f"scanner={sum(1 for u in universe if u.source=='scanner_breakout')} "
        f"strong={sum(1 for u in universe if u.source=='strong_breakout')} "
        f"fakeout={sum(1 for u in universe if u.source=='fakeout')}",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    errors = 0
    for i, u in enumerate(universe, 1):
        assert u.entry_price is not None
        print(
            f"[{i}/{len(universe)}] {u.source} {u.decision_ts.isoformat()} "
            f"entry={u.entry_price} ...",
            flush=True,
        )
        try:
            rec = analyze_signal_clusters(
                symbol,
                decision_ts=u.decision_ts,
                entry_price=float(u.entry_price),
                tier=u.tier,
                confirm_delta=u.confirm_delta,
                followthrough_delta=u.followthrough_delta,
                gap_pct=float(args.gap_pct),
            )
            rec["source"] = u.source
        except Exception as exc:  # noqa: BLE001
            errors += 1
            rec = {
                "signal_ts": u.decision_ts.isoformat(),
                "source": u.source,
                "entry_price": u.entry_price,
                "error": str(exc),
            }
            print(f"  ERROR: {exc}", flush=True)
        results.append(rec)
        if not rec.get("error"):
            print(
                f"  outcome={rec.get('outcome')} max=+{rec.get('max_exc_pct'):.2f}% "
                f"clusters {rec.get('n_reached')}/{rec.get('n_clusters')}",
                flush=True,
            )

    overall = summarize_cross_signal(results)
    by_source: dict[str, Any] = {}
    for src in sorted({r.get("source") for r in results if r.get("source")}):
        by_source[str(src)] = summarize_cross_signal(
            [r for r in results if r.get("source") == src]
        )

    hypotheses = derive_tp_hypotheses(overall, by_source)
    payload = {
        "symbol": symbol,
        "scan_from": SCAN_FROM.isoformat(),
        "scan_to": SCAN_TO.isoformat(),
        "gap_pct": float(args.gap_pct),
        "n_universe": len(universe),
        "n_analyzed": len(results),
        "n_errors": errors,
        "summary": overall,
        "by_source": by_source,
        "tp_hypotheses": hypotheses,
        "json_out": str(args.out),
        "signals": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_markdown_report(args.md_out, payload=payload, hypotheses=hypotheses)

    print("\n=== TP HYPOTHESES ===")
    for h in hypotheses:
        print(f"- {h}")
    print(f"wrote {args.out}")
    print(f"wrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
