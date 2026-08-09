"""Export multi-coin overlap artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_multicoin_overlap import DEFINITIONS_DOC


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        t = x.tz_convert("UTC") if x.tzinfo else x.tz_localize("UTC")
        return t.strftime("%Y-%m-%d %H:%M:%S UTC")
    if hasattr(x, "item"):
        try:
            return x.item()
        except Exception:
            pass
    if isinstance(x, float) and (x != x):
        return None
    return x


def render_summary_md(p: dict[str, Any]) -> str:
    d = p["decision"]
    t = p["timeline"]
    fa = p["idle_fill_apt_by_doge"]
    fd = p["idle_fill_doge_by_apt"]
    sa = p["shared_apt_first"]
    m1 = p["capital"]["M1_SINGLE_APT"]
    m2 = p["capital"]["M2_SHARED_SLOT_APT_FIRST"]
    m3 = p["capital"]["M3_PARALLEL_50_50"]
    lines = [
        "# Multi-Coin Idle-Fill / Overlap Audit (APT + DOGE)",
        "",
        f"- Audit: `{p['audit_version']}`",
        f"- Window: {_jsonable(p['span_start'])} → {_jsonable(p['span_end'])}",
        f"- Independent trades: **{p['sources']['independent_n']}** (cache `{p['sources']['independent_trades_file']}`)",
        f"- Global reference trades: **{p['sources']['global_n']}**",
        "",
        f"## Primary Decision: **{d['decision']}**",
        "",
        *[f"- {r}" for r in d["reasons"]],
        "",
        "## Answers (compact)",
        "",
        f"1. APT idle filled by DOGE: **{(fa['idle_fill_ratio'] or 0)*100:.1f}%**",
        f"2. DOGE idle filled by APT: **{(fd['idle_fill_ratio'] or 0)*100:.1f}%**",
        f"3. Both active: **{t['pct_both_active']:.1f}%**",
        f"4. Both flat: **{t['pct_both_flat']:.1f}%**",
        f"5. Extra shared-slot trades vs APT-only: **{p['extra_trades_shared_vs_apt']}** "
        f"(executed {sa['executed']} / candidates {sa['candidates']})",
        f"6. Blocked (APT_FIRST): **{sa['blocked']}** (block rate {100*(sa['block_rate'] or 0):.1f}%)",
        f"7. Time any position (parallel union): **{t['time_any_position_pct']:.1f}%** "
        f"(APT-only TIM {p['single_coin_stats'].set_index('label').loc['APT_INDEPENDENT','time_in_market_pct']:.1f}%)",
        f"8. Net PnL same capital — M1 APT {m1['net_return_additive']:.1f} → "
        f"M2 shared {m2['net_return_additive']:.1f} → M3 parallel 50/50 {m3['net_return_additive']:.1f} "
        f"(unscaled dual-notional {p['capital']['M3_PARALLEL_UNSCALED_DUAL_NOTIONAL']['net_return_additive']:.1f} — unfair)",
        f"9. PnL/day: M1 {m1['pnl_per_day']:.4f} → M2 {m2['pnl_per_day']:.4f} → M3 {m3['pnl_per_day']:.4f}",
        f"10. PnL/capital-hour: M1 {m1['pnl_per_capital_hour']:.4f} → M2 {m2['pnl_per_capital_hour']:.4f} → M3 {m3['pnl_per_capital_hour']:.4f}",
        f"11. Independence: see signal_correlation.csv (60m APT coincidence "
        f"{d.get('apt_coincidence_60m_pct')}%)",
        f"12. Second coin useful? See decision — capital-efficient idle fill is the criterion.",
        "",
        "## Conclusion",
        "",
        _conclusion(d, fa, t, sa, m1, m2, m3),
        "",
    ]
    return "\n".join(lines)


def _conclusion(d, fa, t, sa, m1, m2, m3) -> str:
    return (
        f"**{d['decision']}**. DOGE fills about {(fa['idle_fill_ratio'] or 0)*100:.0f}% of APT's flat "
        f"calendar time; both coins are simultaneously active {t['pct_both_active']:.0f}% of the span "
        f"and simultaneously flat {t['pct_both_flat']:.0f}%. A shared single slot executes {sa['executed']} "
        f"trades and blocks {sa['blocked']} ({100*(sa['block_rate'] or 0):.0f}% block rate). "
        f"On identical total capital, shared-slot additive net is {m2['net_return_additive']:.0f} vs "
        f"APT-only {m1['net_return_additive']:.0f}; parallel 50/50 scales to {m3['net_return_additive']:.0f} "
        f"(unscaled dual notional overstates the edge). "
        f"Extra coins help only insofar as they fill fragmented idle without requiring permanent 2× capital."
    )


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    def wdf(name: str, df: pd.DataFrame) -> Path:
        p = out_dir / name
        df.to_csv(p, index=False)
        paths[name] = p
        return p

    wdf("single_coin_stats.csv", payload["single_coin_stats"])
    wdf(
        "timeline_state_stats.csv",
        pd.DataFrame([{**payload["timeline"], **{f"occ_{k}": v for k, v in payload["occupancy"].items() if k.startswith('pct_') or k.startswith('avg_')}}]),
    )
    wdf(
        "idle_fill_stats.csv",
        pd.DataFrame([payload["idle_fill_apt_by_doge"], payload["idle_fill_doge_by_apt"]]),
    )
    wdf("near_simultaneous_entries.csv", payload["near_simultaneous"])
    wdf("trade_overlap.csv", payload["near_sim_buckets"])
    # also entry overlap counts
    wdf(
        "entry_overlap_counts.csv",
        pd.DataFrame([payload["entry_overlap_counts"]]),
    )

    shared_rows = []
    for key, label in (
        ("shared_apt_first", "APT_FIRST"),
        ("shared_doge_first", "DOGE_FIRST"),
    ):
        s = payload[key]
        shared_rows.append({"tie_break": label, **{k: v for k, v in s.items() if not isinstance(v, (dict, list))}})
    wdf("shared_slot_results.csv", pd.DataFrame(shared_rows))

    wdf(
        "parallel_results.csv",
        pd.DataFrame([{**payload["parallel"], **payload["occupancy"]}]),
    )

    if payload["shared_blocked_apt_first"] is not None and len(payload["shared_blocked_apt_first"]):
        wdf("blocked_trade_analysis.csv", payload["shared_blocked_apt_first"])
    else:
        wdf("blocked_trade_analysis.csv", pd.DataFrame())

    wdf("blocked_hold_buckets.csv", payload["blocked_buckets"])
    wdf("signal_correlation.csv", payload["signal_corr"])

    cap_rows = []
    for k, v in payload["capital"].items():
        row = {"key": k, **{kk: vv for kk, vv in v.items() if not isinstance(vv, (pd.DataFrame,))}}
        cap_rows.append(row)
    wdf("capital_efficiency.csv", pd.DataFrame(cap_rows))

    # timeline events sample from shared executed + blocked
    ev_rows = []
    for _, r in payload["shared_executed_apt_first"].iterrows():
        ev_rows.append(
            {
                "timestamp": r["entry_time"],
                "symbol": r["symbol"],
                "event": "ENTRY_EXECUTED",
                "trade_id": r["trade_id"],
                "direction": r["side"],
                "scheduler_state": "SHARED_APT_FIRST",
            }
        )
        ev_rows.append(
            {
                "timestamp": r["exit_time"],
                "symbol": r["symbol"],
                "event": "EXIT",
                "trade_id": r["trade_id"],
                "direction": r["side"],
                "scheduler_state": "SHARED_APT_FIRST",
            }
        )
    for _, r in payload["shared_blocked_apt_first"].iterrows():
        ev_rows.append(
            {
                "timestamp": r["entry_time"],
                "symbol": r["symbol"],
                "event": "ENTRY_BLOCKED",
                "trade_id": r["trade_id"],
                "direction": r["side"],
                "scheduler_state": "SHARED_APT_FIRST",
            }
        )
    ev = pd.DataFrame(ev_rows)
    if len(ev):
        ev = ev.sort_values(["timestamp", "event"]).reset_index(drop=True)
    wdf("timeline_events.csv", ev)

    p = out_dir / "DEFINITIONS.md"
    p.write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    paths["definitions"] = p

    p = out_dir / "summary.md"
    p.write_text(render_summary_md(payload), encoding="utf-8")
    paths["summary_md"] = p

    summary = {
        "audit_version": payload["audit_version"],
        "span_start": _jsonable(payload["span_start"]),
        "span_end": _jsonable(payload["span_end"]),
        "sources": payload["sources"],
        "decision": payload["decision"],
        "timeline": payload["timeline"],
        "idle_fill_apt_by_doge": payload["idle_fill_apt_by_doge"],
        "idle_fill_doge_by_apt": payload["idle_fill_doge_by_apt"],
        "shared_apt_first": payload["shared_apt_first"],
        "shared_doge_first": payload["shared_doge_first"],
        "shared_vs_global": payload["shared_vs_global"],
        "capital": payload["capital"],
        "entry_overlap_counts": payload["entry_overlap_counts"],
        "blocked_mix": payload["blocked_mix"],
        "extra_trades_shared_vs_apt": payload["extra_trades_shared_vs_apt"],
        "extra_trades_shared_vs_doge": payload["extra_trades_shared_vs_doge"],
    }
    p = out_dir / "summary.json"
    p.write_text(json.dumps(_jsonable(summary), indent=2) + "\n", encoding="utf-8")
    paths["summary_json"] = p
    return paths
