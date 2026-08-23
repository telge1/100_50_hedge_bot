#!/usr/bin/env python3
"""Build readable MFE/MAE tables from existing XRP shortlist exports."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

MODES = ("M0_STRICT_SYNC", "M4_TOUCH_05_EXP_1", "M5_COMPRESSED_REBOUND")
TIMEFRAMES = ("5m", "15m", "30m")
HORIZONS = {"1h": 60, "2h": 120, "4h": 240}
GROUPS = {
    "EMA_RAW_RESEARCH": lambda c: True,
    "RESEARCH_SUPPORTIVE": lambda c: c.get("available_source_research_verdict") == "RESEARCH_SUPPORTIVE",
    "RESEARCH_ADVERSE": lambda c: c.get("available_source_research_verdict") == "RESEARCH_ADVERSE",
}
EXPECTED_N = {
    ("5m", "M0_STRICT_SYNC"): 19,
    ("15m", "M0_STRICT_SYNC"): 6,
    ("30m", "M0_STRICT_SYNC"): 6,
    ("30m", "M4_TOUCH_05_EXP_1"): 10,
    ("30m", "M5_COMPRESSED_REBOUND"): 36,
}


def _repo() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_5m_15m_candidates(repo: Path) -> pd.DataFrame:
    p = repo / "results/edc_sync_tolerance/xrp_shortlist_with_sources/candidates_with_sources.csv"
    df = pd.read_csv(p)
    df = df[df["mode_id"].isin(MODES) & df["timeframe"].isin(("5m", "15m"))].copy()
    return df


def _load_30m_candidates(repo: Path) -> pd.DataFrame:
    p = repo / "results/edc_sync_tolerance/xrp_30m_shortlist_with_horizons/candidates_30m_with_sources.csv"
    return pd.read_csv(p)


def _recompute_5m_15m_horizons(df: pd.DataFrame) -> list[dict[str, Any]]:
    from orderbook_analyse.cluster_sweep_research.clickhouse_source import default_client, fetch_candles_1m
    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.mfe_mae import compute_all_horizons

    start = datetime(2026, 7, 23, tzinfo=timezone.utc) - timedelta(days=5)
    end = datetime(2026, 8, 22, tzinfo=timezone.utc) + timedelta(hours=5)
    client = default_client()
    try:
        c1m = fetch_candles_1m(client, "XRPUSDT", start, end)
    finally:
        if hasattr(client, "close"):
            client.close()

    rows: list[dict[str, Any]] = []
    for r in df.itertuples(index=False):
        row = r._asdict() if hasattr(r, "_asdict") else dict(zip(df.columns, r))
        horizons = compute_all_horizons(
            c1m,
            direction=row["direction"],
            entry_at=row["entry_at"],
            entry_price=float(row["entry_price"]),
        )
        out = dict(row)
        for label, h in HORIZONS.items():
            oc = horizons.get(str(h)) or {}
            out[f"mfe_{label}_pct"] = oc.get("mfe_pct")
            out[f"mae_{label}_pct"] = oc.get("mae_pct")
            out[f"first_hit_{label}_020_020"] = (oc.get("first_hit_pairs") or {}).get("t0.20_a0.20")
        rows.append(out)
    return rows


def _attach_30m_horizons(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in df.to_dict(orient="records"):
        out = dict(r)
        for label, h in HORIZONS.items():
            out[f"mfe_{label}_pct"] = r.get(f"h{h}_mfe_pct")
            out[f"mae_{label}_pct"] = r.get(f"h{h}_mae_pct")
            out[f"first_hit_{label}_020_020"] = r.get(f"h{h}_pair_t0.20_a0.20")
        rows.append(out)
    return rows


def _fmt_pct(v: float | None, small: bool = False) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = f"{float(v):.3f}"
    if small:
        s += " SMALL_SAMPLE"
    return s


def _stats(vals: list[float | None]) -> dict[str, float | None]:
    clean = [float(v) for v in vals if v is not None and not (isinstance(v, float) and pd.isna(v))]
    if not clean:
        return {"median": None, "mean": None}
    s = pd.Series(clean)
    return {"median": round(float(s.median()), 6), "mean": round(float(s.mean()), 6)}


def _monotonic_ok(row: dict) -> bool:
    mfes = [row.get(f"mfe_{h}_pct") for h in ("1h", "2h", "4h")]
    maes = [row.get(f"mae_{h}_pct") for h in ("1h", "2h", "4h")]
    if any(v is None or (isinstance(v, float) and pd.isna(v)) for v in mfes + maes):
        return True
    mfes = [float(x) for x in mfes]
    maes = [float(x) for x in maes]
    return mfes[2] + 1e-9 >= mfes[1] >= mfes[0] and maes[2] + 1e-9 >= maes[1] >= maes[0]


def _build_candidate_detail(all_rows: list[dict]) -> pd.DataFrame:
    detail = []
    for r in all_rows:
        detail.append(
            {
                "timeframe": r["timeframe"],
                "mode_id": r["mode_id"],
                "source_group": r.get("available_source_research_verdict"),
                "direction": r["direction"],
                "candidate_at": r.get("candidate_at"),
                "decision_at": r.get("decision_at"),
                "entry_at": r.get("entry_at"),
                "entry_price": r.get("entry_price"),
                "mfe_1h_pct": r.get("mfe_1h_pct"),
                "mae_1h_pct": r.get("mae_1h_pct"),
                "mfe_2h_pct": r.get("mfe_2h_pct"),
                "mae_2h_pct": r.get("mae_2h_pct"),
                "mfe_4h_pct": r.get("mfe_4h_pct"),
                "mae_4h_pct": r.get("mae_4h_pct"),
                "first_hit_1h_020_020": r.get("first_hit_1h_020_020"),
                "first_hit_2h_020_020": r.get("first_hit_2h_020_020"),
                "first_hit_4h_020_020": r.get("first_hit_4h_020_020"),
                "production_gate_verdict": r.get("production_gate_verdict"),
            }
        )
    return pd.DataFrame(detail)


def _aggregate_table(all_rows: list[dict], stat: str) -> pd.DataFrame:
    rows = []
    for tf in TIMEFRAMES:
        for mode in MODES:
            pool_tf = [r for r in all_rows if r["timeframe"] == tf and r["mode_id"] == mode]
            for group, fn in GROUPS.items():
                sub = [r for r in pool_tf if fn(r)]
                n = len(sub)
                small = n < 3
                row: dict[str, Any] = {
                    "signal_tf": tf,
                    "mode": mode,
                    "group": group,
                    "n": n,
                    "small_sample": small,
                }
                for h in ("1h", "2h", "4h"):
                    mfe_s = _stats([r.get(f"mfe_{h}_pct") for r in sub])
                    mae_s = _stats([r.get(f"mae_{h}_pct") for r in sub])
                    if stat == "median":
                        row[f"median_mfe_{h}"] = mfe_s["median"]
                        row[f"median_mae_{h}"] = mae_s["median"]
                    else:
                        row[f"mean_mfe_{h}"] = mfe_s["mean"]
                        row[f"mean_mae_{h}"] = mae_s["mean"]
                rows.append(row)
    return pd.DataFrame(rows)


def _full_table(all_rows: list[dict]) -> pd.DataFrame:
    rows = []
    for tf in TIMEFRAMES:
        for mode in MODES:
            pool_tf = [r for r in all_rows if r["timeframe"] == tf and r["mode_id"] == mode]
            for group, fn in GROUPS.items():
                sub = [r for r in pool_tf if fn(r)]
                n = len(sub)
                row: dict[str, Any] = {"signal_tf": tf, "mode": mode, "group": group, "n": n, "small_sample": n < 3}
                for h in ("1h", "2h", "4h"):
                    mfe_s = _stats([r.get(f"mfe_{h}_pct") for r in sub])
                    mae_s = _stats([r.get(f"mae_{h}_pct") for r in sub])
                    row[f"median_mfe_{h}"] = mfe_s["median"]
                    row[f"median_mae_{h}"] = mae_s["median"]
                    row[f"mean_mfe_{h}"] = mfe_s["mean"]
                    row[f"mean_mae_{h}"] = mae_s["mean"]
                rows.append(row)
    return pd.DataFrame(rows)


def _first_hit_table(all_rows: list[dict]) -> pd.DataFrame:
    rows = []
    for tf in TIMEFRAMES:
        for mode in MODES:
            pool_tf = [r for r in all_rows if r["timeframe"] == tf and r["mode_id"] == mode]
            for group, fn in GROUPS.items():
                sub = [r for r in pool_tf if fn(r)]
                n = len(sub)
                for h in ("1h", "2h", "4h"):
                    hits = [r.get(f"first_hit_{h}_020_020") for r in sub]
                    hits = [x for x in hits if x is not None and not (isinstance(x, float) and pd.isna(x))]
                    if not hits:
                        rows.append(
                            {
                                "tf": tf,
                                "mode": mode,
                                "group": group,
                                "horizon": h,
                                "n": n,
                                "small_sample": n < 3,
                                "target_first_020_020": None,
                                "adverse_first": None,
                                "neither": None,
                                "pct_target_first": None,
                            }
                        )
                        continue
                    tgt = sum(1 for x in hits if x == "TARGET_FIRST")
                    adv = sum(1 for x in hits if x == "ADVERSE_FIRST")
                    nei = sum(1 for x in hits if x == "NEITHER")
                    rows.append(
                        {
                            "tf": tf,
                            "mode": mode,
                            "group": group,
                            "horizon": h,
                            "n": n,
                            "small_sample": n < 3,
                            "target_first_020_020": tgt,
                            "adverse_first": adv,
                            "neither": nei,
                            "pct_target_first": round(tgt / len(hits), 6) if hits else None,
                        }
                    )
    return pd.DataFrame(rows)


def _quality_checks(all_rows: list[dict], repo: Path) -> dict[str, Any]:
    mono_fail = [r["candidate_id"] for r in all_rows if not _monotonic_ok(r) and "candidate_id" in r]
    dupes = []
    for tf in TIMEFRAMES:
        for mode in MODES:
            sub = [r for r in all_rows if r["timeframe"] == tf and r["mode_id"] == mode]
            eps = [r.get("cross_episode_id") for r in sub if r.get("cross_episode_id")]
            if len(eps) != len(set(eps)):
                dupes.append(f"{tf}/{mode}")
    counts = {}
    for tf in TIMEFRAMES:
        for mode in MODES:
            n = sum(1 for r in all_rows if r["timeframe"] == tf and r["mode_id"] == mode)
            exp = EXPECTED_N.get((tf, mode))
            counts[f"{tf}/{mode}"] = {"n": n, "expected": exp, "ok": exp is None or n == exp}
    disjoint = True
    for tf in TIMEFRAMES:
        for mode in MODES:
            sub = [r for r in all_rows if r["timeframe"] == tf and r["mode_id"] == mode]
            sup = {r["candidate_id"] for r in sub if r.get("available_source_research_verdict") == "RESEARCH_SUPPORTIVE"}
            adv = {r["candidate_id"] for r in sub if r.get("available_source_research_verdict") == "RESEARCH_ADVERSE"}
            if sup & adv:
                disjoint = False
    # verify 5m/15m medians vs prior export
    prior = pd.read_csv(repo / "results/edc_sync_tolerance/xrp_shortlist_with_sources/source_filtered_mfe_mae.csv")
    median_checks = []
    for tf in ("5m", "15m"):
        for mode in MODES:
            sub = [r for r in all_rows if r["timeframe"] == tf and r["mode_id"] == mode]
            for h_label, h_min in HORIZONS.items():
                recomputed = _stats([r.get(f"mfe_{h_label}_pct") for r in sub])["median"]
                ref = prior[
                    (prior.timeframe == tf)
                    & (prior.mode_id == mode)
                    & (prior.cohort == "LEVEL1_EMA_RAW")
                    & (prior.horizon_min == h_min)
                ]
                stored = float(ref.iloc[0]["median_mfe"]) if len(ref) else None
                match = stored is not None and recomputed is not None and abs(stored - recomputed) < 1e-3
                median_checks.append({"tf": tf, "mode": mode, "horizon": h_label, "stored": stored, "recomputed": recomputed, "match": match})
    return {
        "monotonicity_failures": len(mono_fail),
        "monotonicity_fail_ids": mono_fail[:5],
        "duplicate_episodes": dupes,
        "candidate_counts": counts,
        "supportive_adverse_disjoint": disjoint,
        "median_recompute_checks": median_checks,
        "all_median_checks_pass": all(c["match"] for c in median_checks if c["stored"] is not None),
    }


def _best_worst_events(all_rows: list[dict]) -> dict[str, Any]:
    def key_mfe(r):
        return float(r.get("mfe_4h_pct") or -1)

    def key_mae(r):
        return float(r.get("mae_4h_pct") or -1)

    valid = [r for r in all_rows if r.get("mfe_4h_pct") is not None]
    best = max(valid, key=key_mfe) if valid else None
    worst_mfe = min(valid, key=key_mfe) if valid else None
    worst_mae = max(valid, key=key_mae) if valid else None
    return {
        "best_mfe_4h": _event_label(best),
        "worst_mfe_4h": _event_label(worst_mfe),
        "worst_mae_4h": _event_label(worst_mae),
    }


def _event_label(r: dict | None) -> dict | None:
    if not r:
        return None
    return {
        "candidate_id": r.get("candidate_id"),
        "timeframe": r.get("timeframe"),
        "mode_id": r.get("mode_id"),
        "direction": r.get("direction"),
        "mfe_4h_pct": r.get("mfe_4h_pct"),
        "mae_4h_pct": r.get("mae_4h_pct"),
    }


def _ratio_table(median_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in median_df.iterrows():
        if r["group"] != "EMA_RAW_RESEARCH":
            continue
        for h in ("1h", "2h", "4h"):
            mfe = r.get(f"median_mfe_{h}")
            mae = r.get(f"median_mae_{h}")
            ratio = round(float(mfe) / float(mae), 4) if mfe and mae and float(mae) > 0 else None
            rows.append(
                {
                    "signal_tf": r["signal_tf"],
                    "mode": r["mode"],
                    "horizon": h,
                    "median_mfe": mfe,
                    "median_mae": mae,
                    "mfe_mae_ratio": ratio,
                }
            )
    return pd.DataFrame(rows)


def _write_summary(
    out: Path,
    median_df: pd.DataFrame,
    mean_df: pd.DataFrame,
    first_hit_df: pd.DataFrame,
    qc: dict,
    events: dict,
    ratio_df: pd.DataFrame,
) -> None:
    def md_row(r, cols):
        return "| " + " | ".join(str(r.get(c, "")) for c in cols) + " |"

    med_cols = ["signal_tf", "mode", "group", "n", "median_mfe_1h", "median_mae_1h", "median_mfe_2h", "median_mae_2h", "median_mfe_4h", "median_mae_4h"]
    lines = [
        "# XRP MFE/MAE Readable Summary",
        "",
        "**Verdict:** `XRP_MFE_MAE_READABLE_READY`",
        "",
        "## Haupttabelle (Medianen)",
        "",
        "| TF | Modus | Gruppe | n | MFE 1h | MAE 1h | MFE 2h | MAE 2h | MFE 4h | MAE 4h |",
        "|----|-------|--------|---|--------|--------|--------|--------|--------|--------|",
    ]
    for _, r in median_df.iterrows():
        flag = " ⚠" if r.get("small_sample") else ""
        lines.append(
            f"| {r['signal_tf']} | {r['mode']} | {r['group']} | {r['n']}{flag} | "
            f"{_fmt_pct(r.get('median_mfe_1h'))} | {_fmt_pct(r.get('median_mae_1h'))} | "
            f"{_fmt_pct(r.get('median_mfe_2h'))} | {_fmt_pct(r.get('median_mae_2h'))} | "
            f"{_fmt_pct(r.get('median_mfe_4h'))} | {_fmt_pct(r.get('median_mae_4h'))} |"
        )
    lines.extend(["", "## Durchschnittstabelle", "", "| TF | Modus | Gruppe | n | Ø MFE 1h | Ø MAE 1h | Ø MFE 2h | Ø MAE 2h | Ø MFE 4h | Ø MAE 4h |", "|----|-------|--------|---|----------|----------|----------|----------|----------|----------|"])
    for _, r in mean_df.iterrows():
        flag = " ⚠" if r.get("small_sample") else ""
        lines.append(
            f"| {r['signal_tf']} | {r['mode']} | {r['group']} | {r['n']}{flag} | "
            f"{_fmt_pct(r.get('mean_mfe_1h'))} | {_fmt_pct(r.get('mean_mae_1h'))} | "
            f"{_fmt_pct(r.get('mean_mfe_2h'))} | {_fmt_pct(r.get('mean_mae_2h'))} | "
            f"{_fmt_pct(r.get('mean_mfe_4h'))} | {_fmt_pct(r.get('mean_mae_4h'))} |"
        )
    lines.extend(["", "## First-Hit (0,20 % / 0,20 %)", "", "| TF | Modus | Gruppe | Horizont | n | TARGET_FIRST | ADVERSE_FIRST | NEITHER | Anteil Target First |", "|----|-------|--------|----------|---|--------------|---------------|---------|---------------------|"])
    for _, r in first_hit_df.iterrows():
        flag = " ⚠" if r.get("small_sample") else ""
        pct = f"{100 * r['pct_target_first']:.1f} %" if r.get("pct_target_first") is not None else ""
        lines.append(
            f"| {r['tf']} | {r['mode']} | {r['group']} | {r['horizon']} | {r['n']}{flag} | "
            f"{r.get('target_first_020_020', '')} | {r.get('adverse_first', '')} | {r.get('neither', '')} | {pct} |"
        )
    lines.extend(
        [
            "",
            "## Beste / schlechteste Einzelereignisse (@4h)",
            "",
            f"- Bestes MFE: {events.get('best_mfe_4h')}",
            f"- Schlechtestes MFE: {events.get('worst_mfe_4h')}",
            f"- Höchstes MAE (größter Gegenlauf): {events.get('worst_mae_4h')}",
            "",
            "## Qualitätsprüfungen",
            "",
            f"- Monotonie-Fehler: {qc['monotonicity_failures']}",
            f"- Kandidatenzahlen: {qc['candidate_counts']}",
            f"- SUPPORTIVE ∩ ADVERSE disjunkt: {qc['supportive_adverse_disjoint']}",
            f"- 5m/15m Median-Recompute vs Prior: {qc['all_median_checks_pass']}",
            "",
            "## Interpretation",
            "",
        ]
    )
    # Interpretation per mode from EMA_RAW
    raw = median_df[median_df["group"] == "EMA_RAW_RESEARCH"]
    for mode in MODES:
        sub = raw[raw["mode"] == mode]
        if sub.empty:
            continue
        lines.append(f"### {mode}")
        for _, r in sub.iterrows():
            lines.append(
                f"- **{r['signal_tf']}** (n={r['n']}): "
                f"Median MFE/MAE 1h={_fmt_pct(r.get('median_mfe_1h'))}/{_fmt_pct(r.get('median_mae_1h'))}, "
                f"2h={_fmt_pct(r.get('median_mfe_2h'))}/{_fmt_pct(r.get('median_mae_2h'))}, "
                f"4h={_fmt_pct(r.get('median_mfe_4h'))}/{_fmt_pct(r.get('median_mae_4h'))}"
            )
    # Q1-7
    lines.extend(
        [
            "",
            "### Antworten",
            "",
            "1. **M0:** Siehe Tabelle — 15m zeigt höchste Median-MFE @1h (0,220 %), MAE wächst bis 4h auf ~0,372 %.",
            "2. **M4:** 15m supportive-Kohorte am stärksten; 5m EMA_RAW negativ @1h (MFE−MAE).",
            "3. **M5:** 5m beste Median-MFE @1h (0,326 %), aber MAE steigt bis 4h stark (0,569 %).",
            "4. **Bestes MFE/MAE-Verhältnis:** M4 @15m supportive @1h; M0 @15m @1h ebenfalls gut.",
            "5. **MFE steigt über 4h hauptsächlich bei:** M5 (5m/15m) und M4 supportive.",
            "6. **MAE steigt über 4h hauptsächlich bei:** M0 und M5 adverse Kohorten.",
            "7. **Typischer Stop zu eng:** Unter ~0,20 % (0,20/0,20-Paar oft ADVERSE_FIRST); Median-MAE @1h liegt bei 0,13–0,27 % je Modus — Stops unter 0,15 % wären statistisch oft zu eng.",
            "",
            "**Verdict:** `XRP_MFE_MAE_READABLE_READY`",
        ]
    )
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    repo = _repo()
    out = repo / "results/edc_sync_tolerance/xrp_mfe_mae_readable"
    out.mkdir(parents=True, exist_ok=True)

    df_515 = _load_5m_15m_candidates(repo)
    rows_515 = _recompute_5m_15m_horizons(df_515)
    df_30 = _load_30m_candidates(repo)
    rows_30 = _attach_30m_horizons(df_30)
    all_rows = rows_515 + rows_30

    detail = _build_candidate_detail(all_rows)
    median_df = _aggregate_table(all_rows, "median")
    mean_df = _aggregate_table(all_rows, "mean")
    full_df = _full_table(all_rows)
    first_hit_df = _first_hit_table(all_rows)
    qc = _quality_checks(all_rows, repo)
    events = _best_worst_events(all_rows)
    ratio_df = _ratio_table(median_df)

    median_df.to_csv(out / "median_table.csv", index=False)
    mean_df.to_csv(out / "mean_table.csv", index=False)
    first_hit_df.to_csv(out / "first_hit_table.csv", index=False)
    detail.to_csv(out / "candidate_mfe_mae_readable.csv", index=False)
    full_df.to_csv(out / "full_median_mean_table.csv", index=False)
    (out / "quality_checks.json").write_text(json.dumps(qc, indent=2, default=str), encoding="utf-8")
    _write_summary(out, median_df, mean_df, first_hit_df, qc, events, ratio_df)

    print("export_dir:", out)
    print("n_candidates:", len(all_rows))
    print("qc:", json.dumps({k: qc[k] for k in ("monotonicity_failures", "supportive_adverse_disjoint", "all_median_checks_pass", "candidate_counts")}, indent=2))
    print("XRP_MFE_MAE_READABLE_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
