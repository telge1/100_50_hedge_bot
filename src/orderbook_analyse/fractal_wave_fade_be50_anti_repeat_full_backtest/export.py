"""Export anti-repeat A/B results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_be50_anti_repeat_full_backtest import DEFINITIONS_DOC


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items() if k not in ("episodes",)}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        t = x.tz_convert("UTC") if x.tzinfo else x.tz_localize("UTC")
        return t.strftime("%Y-%m-%d %H:%M:%S UTC")
    if isinstance(x, pd.DataFrame):
        return f"<DataFrame n={len(x)}>"
    if hasattr(x, "item"):
        try:
            return x.item()
        except Exception:
            pass
    if isinstance(x, float) and (x != x):
        return None
    return x


def _pct(x, d=2):
    if x is None:
        return "—"
    if abs(x) >= 1e6:
        return f"{x:+.4g}%"
    return f"{x:+.{d}f}%"


def _num(x, d=2):
    if x is None:
        return "—"
    if abs(float(x)) >= 1e6:
        return f"{float(x):.4g}"
    return f"{float(x):.{d}f}"


def _strip_m(m: dict) -> dict:
    out = {}
    for k, v in m.items():
        if k in ("episodes",):
            continue
        if k in ("true_sl", "non_winner"):
            out[k] = {kk: vv for kk, vv in v.items() if kk not in ("top_streaks", "distribution")}
        elif k == "summary":
            out[k] = v
        else:
            out[k] = v
    return out


def render_summary(p: dict[str, Any]) -> str:
    b0, b, a = p["base_m"], p["be50_m"], p["anti_m"]
    bs = p["block_summary"]
    s0, sb, sa = b0["summary"], b["summary"], a["summary"]

    def dlt(x, y):
        if x is None or y is None:
            return None
        return y - x

    lines = [
        f"Primary Decision: **{p['decision']}**",
        "",
        "| Metric | BE50 | BE50 + Anti-Repeat | Delta |",
        "| ---------------------- | ------: | -----------------: | ----: |",
        f"| End Equity | {_num(sb['end_total'])} | {_num(sa['end_total'])} | {_num(dlt(sb['end_total'], sa['end_total']))} |",
        f"| Total Return | {_pct(sb['performance_pct'])} | {_pct(sa['performance_pct'])} | {_pct(dlt(sb['performance_pct'], sa['performance_pct']))} |",
        f"| Max DD | {_pct(b['max_dd'])} | {_pct(a['max_dd'])} | {dlt(b['max_dd'], a['max_dd']):+.2f}pp |",
        f"| >=10% DD episodes | {b['n_ge_10_dd']} | {a['n_ge_10_dd']} | {a['n_ge_10_dd']-b['n_ge_10_dd']:+d} |",
        f"| Longest TRUE SL streak | {b['true_sl']['max_streak']} | {a['true_sl']['max_streak']} | {a['true_sl']['max_streak']-b['true_sl']['max_streak']:+d} |",
        f"| 3+ SL streaks | {b['true_sl']['n_ge_3']} | {a['true_sl']['n_ge_3']} | {a['true_sl']['n_ge_3']-b['true_sl']['n_ge_3']:+d} |",
        f"| 5+ SL streaks | {b['true_sl']['n_ge_5']} | {a['true_sl']['n_ge_5']} | {a['true_sl']['n_ge_5']-b['true_sl']['n_ge_5']:+d} |",
        f"| Trades blocked | — | {bs['n_blocked']} | — |",
        f"| SL avoided | — | {bs['sl_avoided']} | — |",
        f"| TP lost | — | {bs['tp_lost']} | — |",
        "",
        f"- Frozen baseline: `{p['frozen_dir']}`",
        f"- Reset rule: intervening opposite wave on SL `first_signal_tf` "
        f"(DOWN after SHORT SL / UP after LONG SL); BE/TP do not arm the block",
        f"- BE avoided (blocked): {bs['be_avoided']}",
        f"- avoided_loss_pp={bs['avoided_loss_pp']:+.2f} lost_profit_pp={bs['lost_profit_pp']:+.2f} "
        f"net_block_effect_pp={bs['net_block_effect_pp']:+.2f}",
        f"- Resets recorded: {len(p['resets'])}",
        "",
        "## Three-way comparison",
        "",
        "| Metric | Baseline | BE50 | BE50 + Anti-Repeat |",
        "| --- | ---: | ---: | ---: |",
        f"| Trades | {s0.get('n_tp',0)+s0.get('n_sl',0)+s0.get('n_be',0)+s0.get('n_other',0)} | "
        f"{sb.get('n_tp',0)+sb.get('n_sl',0)+sb.get('n_be',0)+sb.get('n_other',0)} | "
        f"{sa.get('n_tp',0)+sa.get('n_sl',0)+sa.get('n_be',0)+sa.get('n_other',0)} |",
        f"| TP | {s0['n_tp']} | {sb['n_tp']} | {sa['n_tp']} |",
        f"| SL | {s0['n_sl']} | {sb['n_sl']} | {sa['n_sl']} |",
        f"| BE | {s0['n_be']} | {sb['n_be']} | {sa['n_be']} |",
        f"| Blocked | — | — | {bs['n_blocked']} |",
        f"| End Equity | {_num(s0['end_total'])} | {_num(sb['end_total'])} | {_num(sa['end_total'])} |",
        f"| Total Return | {_pct(s0['performance_pct'])} | {_pct(sb['performance_pct'])} | {_pct(sa['performance_pct'])} |",
        f"| Max DD | {_pct(b0['max_dd'])} | {_pct(b['max_dd'])} | {_pct(a['max_dd'])} |",
        f"| Longest TRUE SL | {b0['true_sl']['max_streak']} | {b['true_sl']['max_streak']} | {a['true_sl']['max_streak']} |",
        f"| 3+ SL streaks | {b0['true_sl']['n_ge_3']} | {b['true_sl']['n_ge_3']} | {a['true_sl']['n_ge_3']} |",
        f"| 5+ SL streaks | {b0['true_sl']['n_ge_5']} | {b['true_sl']['n_ge_5']} | {a['true_sl']['n_ge_5']} |",
        f"| Longest NON-WINNER | {b0['non_winner']['max_streak']} | {b['non_winner']['max_streak']} | {a['non_winner']['max_streak']} |",
        f"| Return/MaxDD | {_num(abs(s0['performance_pct']/b0['max_dd']) if b0['max_dd'] else None)} | "
        f"{_num(abs(sb['performance_pct']/b['max_dd']) if b['max_dd'] else None)} | "
        f"{_num(abs(sa['performance_pct']/a['max_dd']) if a['max_dd'] else None)} |",
        "",
        "## Large DD episodes",
        "",
        "| Episode | BE50 DD | Anti-Repeat DD | Blocked | SL avoided | TP lost |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, r in p["large_dd"].iterrows():
        lines.append(
            f"| {r['episode']} | {_pct(r['be50_dd_recomputed'])} | {_pct(r['anti_repeat_dd'])} | "
            f"{int(r['repeat_trades_blocked'])} | {int(r['sl_avoided'])} | {int(r['tp_lost'])} |"
        )

    lines += [
        "",
        "## July 2026",
        "",
        f"- BE50: {_pct(p['july_be50']['performance_pct'])}, MaxDD {_pct(p['july_be50']['max_dd_pct'])}, "
        f"longest SL {p['july_be50']['longest_sl_streak']}",
        f"- Anti-Repeat: {_pct(p['july_anti']['performance_pct'])}, MaxDD {_pct(p['july_anti']['max_dd_pct'])}, "
        f"longest SL {p['july_anti']['longest_sl_streak']}",
        "",
        "### Cluster #9–13",
        "",
        "| july_n | trade_id | symbol | side | BE50 | blocked? |",
        "| ---: | ---: | --- | --- | --- | --- |",
    ]
    for _, r in p["july_cluster_9_13"].iterrows():
        lines.append(
            f"| {int(r['july_n'])} | {int(r['trade_id'])} | {r['symbol']} | {r['side']} | "
            f"{r['be50_reason']} {_pct(r['be50_net_pct'])} | {bool(r['blocked_by_anti_repeat'])} |"
        )

    lines += [
        "",
        "## Closing question",
        "",
        (
            "Ja — wiederholte Same-Side-Verluste nach echtem SL werden strukturell blockiert, "
            f"mit {bs['sl_avoided']} vermiedenen SLs vs {bs['tp_lost']} verlorenen TPs "
            f"(net_block_effect {bs['net_block_effect_pp']:+.2f}pp)."
            if bs["sl_avoided"] >= bs["tp_lost"] and p["decision"] in (
                "ANTI_REPEAT_STRONGLY_IMPROVES_RISK",
                "ANTI_REPEAT_IMPROVES_RISK_ADJUSTED",
            )
            else (
                f"Gemischt/Nein — blocked={bs['n_blocked']}, SL avoided={bs['sl_avoided']}, "
                f"TP lost={bs['tp_lost']}, decision={p['decision']}."
            )
        ),
        "",
    ]
    return "\n".join(lines)


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    def w(name: str, df: pd.DataFrame) -> None:
        path = out_dir / name
        df.to_csv(path, index=False)
        paths[name] = path

    w("trade_comparison.csv", payload["trade_comparison"])
    w("blocked_repeat_trades.csv", payload["blocked"])
    w("repeat_reset_events.csv", payload["resets"])
    w("equity_comparison.csv", payload["equity_comparison"])
    w("sl_streak_comparison.csv", payload["sl_streak_comparison"])
    w("drawdown_comparison.csv", payload["drawdown_comparison"])
    w("large_dd_episode_comparison.csv", payload["large_dd"])
    w("monthly_comparison.csv", payload["monthly"])
    if len(payload["post_sl_signals"]):
        w("post_sl_same_side_signals.csv", payload["post_sl_signals"])
    if len(payload["repeat_distance_buckets"]):
        w("repeat_distance_buckets.csv", payload["repeat_distance_buckets"])
    w("july_cluster_9_13.csv", payload["july_cluster_9_13"])

    (out_dir / "DEFINITIONS.md").write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    sm = out_dir / "summary.md"
    sm.write_text(render_summary(payload), encoding="utf-8")
    paths["summary_md"] = sm

    summary = {
        "audit_version": payload["audit_version"],
        "decision": payload["decision"],
        "frozen_dir": payload["frozen_dir"],
        "block_summary": payload["block_summary"],
        "baseline": _strip_m(payload["base_m"]),
        "be50": _strip_m(payload["be50_m"]),
        "be50_anti_repeat": _strip_m(payload["anti_m"]),
        "july_be50": payload["july_be50"],
        "july_anti": payload["july_anti"],
        "n_resets": int(len(payload["resets"])),
    }
    sj = out_dir / "summary.json"
    sj.write_text(json.dumps(_jsonable(summary), indent=2) + "\n", encoding="utf-8")
    paths["summary_json"] = sj
    return paths
