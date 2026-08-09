"""Export drawdown audit artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


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


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:.2f}%"


def _num(x: float | None, d: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x:.{d}f}"


def decision_sentence(decision: str) -> str:
    if decision == "MAX_DD_IS_CLEAR_OUTLIER":
        return "Der -15.13%-Drawdown war Ausreißer."
    if decision == "MAX_DD_IS_RARE_BUT_NOT_UNIQUE":
        return "Der -15.13%-Drawdown war selten aber wiederkehrend."
    if decision == "LARGE_DRAWDOWNS_ARE_RECURRING":
        return (
            "Der -15.13%-Drawdown war typisch für die Tail-Verteilung "
            "(10%+-Drawdowns traten wiederholt auf; die exakte -15%-Tiefe war der Extremwert)."
        )
    return "Drawdown-Baseline konnte nicht reproduziert werden."


def render_summary(p: dict[str, Any]) -> str:
    if p["decision"] == "DRAWDOWN_BASELINE_MISMATCH":
        return (
            "Primary Decision: **DRAWDOWN_BASELINE_MISMATCH**\n\n"
            f"Cause: `{p.get('mismatch')}`\n"
        )

    st = p["stats"]
    c = p["cmp_summary"]
    sl = p["sl_link"]
    dur = p["duration"]
    sp = p["spacing_stats"]

    lines = [
        f"Primary Decision: **{p['decision']}**",
        "",
        "| Metric | BE50 |",
        "| --------------------- | ---: |",
        f"| Max DD | {_pct(st['max_dd'])} |",
        f"| 2nd largest DD | {_pct(st['second_largest_dd'])} |",
        f"| 3rd largest DD | {_pct(st['third_largest_dd'])} |",
        f"| DD >=10% count | {st['n_ge_10']} |",
        f"| DD >=12% count | {st['n_ge_12']} |",
        f"| DD >=14% count | {st['n_ge_14']} |",
        f"| DD >=15% count | {st['n_ge_15']} |",
        f"| p95 DD | {_pct(st['p95_dd'])} |",
        f"| Median >=10% recovery | {_num(st['median_ge10_recovery_hours'])} h |",
        "",
        decision_sentence(p["decision"]),
        "",
        f"- Equity source: `{p['ref_dir']}/equity_comparison.csv` (cashout 30% / reimbursement 100%)",
        f"- BE50 Max-DD reproduction: `{p['be50_max_check']['max_dd_pct']:.6f}%` (expected ≈ -15.134287%)",
        f"- Episodes BE50 / Baseline: **{p['n_episodes_be50']}** / **{p['n_episodes_baseline']}**",
        "",
        "## Threshold counts (BE50)",
        "",
        "| DD threshold | Anzahl Episoden | Anteil aller Episoden |",
        "| ------------ | --------------: | --------------------: |",
    ]
    for _, r in p["threshold_counts"].iterrows():
        lines.append(
            f"| >={r['dd_threshold_pct']:g}% | {int(r['n_episodes'])} | {r['share_of_all_episodes']*100:.2f}% |"
        )

    lines += [
        "",
        "## Tail shape",
        "",
        f"- max_dd = {_pct(st['max_dd'])}",
        f"- second_largest_dd = {_pct(st['second_largest_dd'])}",
        f"- third_largest_dd = {_pct(st['third_largest_dd'])}",
        f"- median_top10_dd = {_pct(st['median_top10_dd'])}",
        f"- p90_dd = {_pct(st['p90_dd'])}",
        f"- p95_dd = {_pct(st['p95_dd'])}",
        f"- p99_dd = {_pct(st['p99_dd'])}",
        "",
        "## Spacing (>=10%)",
        "",
        f"- n={sp.get('ge_10', {}).get('n')}",
        f"- mean gap days={_num(sp.get('ge_10', {}).get('mean_gap_days'))}",
        f"- median gap days={_num(sp.get('ge_10', {}).get('median_gap_days'))}",
        f"- min/max gap days={_num(sp.get('ge_10', {}).get('min_gap_days'))} / {_num(sp.get('ge_10', {}).get('max_gap_days'))}",
        "",
        f"### >=12%: {sp.get('ge_12', {})}",
        f"### >=14%: {sp.get('ge_14', {})}",
        "",
        "## Duration",
        "",
        f"- all: {dur.get('all')}",
        f"- >=5%: {dur.get('ge_5')}",
        f"- >=10%: {dur.get('ge_10')}",
        f"- >=12%: {dur.get('ge_12')}",
        "",
        "## SL / NON-WINNER link (>=10% episodes)",
        "",
        f"- median longest TRUE SL: {_num(sl['median_longest_true_sl'])}",
        f"- median longest NON-WINNER: {_num(sl['median_longest_non_winner'])}",
        f"- mean n_SL / n_BE / n_TP: {_num(sl['mean_n_sl'])} / {_num(sl['mean_n_be'])} / {_num(sl['mean_n_tp'])}",
        f"- share episodes where NON-WINNER streak > TRUE SL streak: {_num(None if sl['share_non_winner_gt_true_sl'] is None else 100*sl['share_non_winner_gt_true_sl'])}%",
        "",
        "## Baseline vs BE50 large-DD frequency",
        "",
        "| DD Threshold | Baseline | BE50 | Delta |",
        "| ------------ | -------: | ---: | ----: |",
    ]
    for _, r in p["baseline_vs_be50"].iterrows():
        lines.append(
            f"| >={r['dd_threshold_pct']:g}% | {int(r['baseline_n'])} | {int(r['be50_n'])} | {int(r['delta_n']):+d} |"
        )
    lines += [
        "",
        f"- Baseline max/2nd/3rd: {_pct(c['baseline_max_dd'])} / {_pct(c['baseline_2nd'])} / {_pct(c['baseline_3rd'])}",
        f"- BE50 max/2nd/3rd: {_pct(c['be50_max_dd'])} / {_pct(c['be50_2nd'])} / {_pct(c['be50_3rd'])}",
        f"- Baseline p95 / BE50 p95: {_pct(c['baseline_p95_dd'])} / {_pct(c['be50_p95_dd'])}",
        f"- Months with >=10% DD (BE50 trough-month): **{p['months_with_ge10']}**",
        "",
        "## Linear leverage sensitivity",
        "",
        "`LINEAR_RISK_APPROXIMATION_ONLY` — no liquidation/margin simulation.",
        "",
        "| Historischer DD | 2x | 3x | 4x | 5x |",
        "| --------------: | -: | -: | -: | -: |",
    ]
    for _, r in p["leverage"].iterrows():
        lines.append(
            f"| {r['metric']}: {_pct(r['historical_dd_pct'])} | {_pct(r['x2'])} | {_pct(r['x3'])} | {_pct(r['x4'])} | {_pct(r['x5'])} |"
        )

    lines += [
        "",
        "## Closing answers",
        "",
        f"1. Max-DD -15.13% nur einmal? **{'Ja' if st['n_ge_15'] <= 1 else 'Nein'}** (n>=15%={st['n_ge_15']})",
        f"2. Zweitgrößter DD: **{_pct(st['second_largest_dd'])}**",
        f"3. Drittgrößter DD: **{_pct(st['third_largest_dd'])}**",
        f"4. >=10%: **{st['n_ge_10']}**",
        f"5. >=12%: **{st['n_ge_12']}**",
        f"6. >=14%: **{st['n_ge_14']}**",
        f"7. >=15%: **{st['n_ge_15']}**",
        f"8. Monate mit >=10%: **{p['months_with_ge10']}**",
        f"9. Abstand >=10%: median {_num(sp.get('ge_10', {}).get('median_gap_days'))} Tage "
        f"(mean {_num(sp.get('ge_10', {}).get('mean_gap_days'))}, "
        f"min {_num(sp.get('ge_10', {}).get('min_gap_days'))}, "
        f"max {_num(sp.get('ge_10', {}).get('max_gap_days'))})",
        f"10. Klassifikation: **{p['decision']}**",
        f"11. BE50 vs Baseline >=10%: {c['baseline_n_ge_10']} → {c['be50_n_ge_10']} "
        f"({c['be50_n_ge_10'] - c['baseline_n_ge_10']:+d})",
        "12. Konservative Planung: nicht p95 aller Episoden (−5%), sondern das wiederkehrende "
        "Large-DD-Band — typisch **~10–14%** (2nd/3rd bzw. Median der Top-10); "
        "Max-DD (−15%) als Stress-Cap, p99 (~−9%) als untere Tail-Referenz über alle Episoden.",
        "",
        decision_sentence(p["decision"]),
        "",
    ]
    return "\n".join(lines)


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    if payload["decision"] == "DRAWDOWN_BASELINE_MISMATCH":
        (out_dir / "summary.md").write_text(render_summary(payload), encoding="utf-8")
        (out_dir / "summary.json").write_text(
            json.dumps(_jsonable(payload), indent=2) + "\n", encoding="utf-8"
        )
        return paths

    def w(name: str, df: pd.DataFrame) -> None:
        path = out_dir / name
        df.to_csv(path, index=False)
        paths[name] = path

    w("be50_drawdown_episodes.csv", payload["be50_episodes"])
    w("top_10_drawdowns.csv", payload["top10"])
    w("drawdown_threshold_counts.csv", payload["threshold_counts"])
    w("monthly_drawdowns.csv", payload["monthly"])
    w("yearly_drawdowns.csv", payload["yearly"])
    w("large_drawdown_spacing.csv", payload["large_spacing"])
    w("baseline_vs_be50_drawdowns.csv", payload["baseline_vs_be50"])
    w("linear_leverage_sensitivity.csv", payload["leverage"])
    # bonus: baseline episodes for transparency
    w("baseline_drawdown_episodes.csv", payload["baseline_episodes"])

    sm = out_dir / "summary.md"
    sm.write_text(render_summary(payload), encoding="utf-8")
    paths["summary_md"] = sm

    summary = {
        "audit_version": payload["audit_version"],
        "decision": payload["decision"],
        "decision_sentence": decision_sentence(payload["decision"]),
        "be50_max_check": payload["be50_max_check"],
        "baseline_max_check": payload["baseline_max_check"],
        "n_episodes_be50": payload["n_episodes_be50"],
        "n_episodes_baseline": payload["n_episodes_baseline"],
        "stats": payload["stats"],
        "spacing_stats": payload["spacing_stats"],
        "duration": payload["duration"],
        "cmp_summary": payload["cmp_summary"],
        "sl_link": payload["sl_link"],
        "months_with_ge10": payload["months_with_ge10"],
        "leverage_note": "LINEAR_RISK_APPROXIMATION_ONLY",
    }
    sj = out_dir / "summary.json"
    sj.write_text(json.dumps(_jsonable(summary), indent=2) + "\n", encoding="utf-8")
    paths["summary_json"] = sj
    return paths
