"""XRP 30d core-sources comparison runner (M0/M4/M5 × 5m/15m/30m)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ...cluster_sweep_research.clickhouse_source import (
    aggregate_timeframe,
    default_client,
    fetch_candles_1m,
    fetch_liquidations,
    fetch_ob_1m,
    fetch_oi_1m,
    fetch_trades_1m,
)
from ...cluster_sweep_research.ema_features import attach_emas, required_warmup_bars
from ..config import EMA_DUAL_CROSS_DEFAULTS, POLICY_VERSION, config_to_dict
from ..coverage_gate import assess_coverage
from ..ema_candidate import attach_atr
from ..episode_state import EpisodeTracker
from ..feature_builder import build_gate_features
from ..models import CandidateType, FinalVerdict
from ..timeframes import bar_close as compute_bar_close
from .core_sources_research_policy import (
    CORE_RESEARCH_POLICY_VERSION,
    apply_core_sources_research,
    apply_production_gate,
    assign_coverage_segment,
    core_research_policy_document,
)
from .coverage_preflight import determine_30d_window
from .mfe_mae import FIRST_HIT_PAIRS, compute_all_horizons
from .mfe_runner import (
    _git_meta,
    _next_open,
    build_mode_catalog,
    detect_for_mode,
    flatten_mfe_row,
)
from .research_policy import compute_all_source_verdicts, map_source_contribution

MODE_IDS = ("M0_STRICT_SYNC", "M4_TOUCH_05_EXP_1", "M5_COMPRESSED_REBOUND")
TIMEFRAMES = ("5m", "15m", "30m")
HORIZON_LABELS = {"1h": 60, "2h": 120, "4h": 240, "30m": 30}

GROUP_MAP: dict[str, Callable[[dict], bool]] = {
    "EMA_RAW": lambda c: True,
    "CORE_RESEARCH_SUPPORTIVE": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_SUPPORTIVE",
    "CORE_RESEARCH_ADVERSE": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_ADVERSE",
    "CORE_RESEARCH_MIXED": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_MIXED",
    "CORE_RESEARCH_INSUFFICIENT": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_INSUFFICIENT",
    "FULL_MULTISOURCE": lambda c: c.get("coverage_segment") == "FULL_MULTISOURCE",
    "PRODUCTION_ALLOW": lambda c: c.get("production_gate_verdict") == "ALLOW",
    "PRODUCTION_BLOCK": lambda c: c.get("production_gate_verdict") == "BLOCK",
    "PRODUCTION_INCONCLUSIVE": lambda c: c.get("production_gate_verdict") == "INCONCLUSIVE_DATA",
}


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def comparison_modes() -> list[dict[str, Any]]:
    catalog = {m["mode_id"]: m for m in build_mode_catalog()}
    return [catalog[m] for m in MODE_IDS]


def evaluate_candidates_core_30d(
    raw_list: list[dict[str, Any]],
    *,
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    window_start: datetime,
    window_end: datetime,
    trades_1m,
    ob_1m,
    oi_1m,
    liq,
    window_report: dict[str, Any] | None,
    mode_id: str,
) -> list[dict[str, Any]]:
    """Thin wrapper → shared canonical evaluator (frozen XRP semantics)."""
    from .shared_strategy.candidates import evaluate_candidates_canonical

    return evaluate_candidates_canonical(
        raw_list,
        df=df,
        symbol=symbol,
        timeframe=timeframe,
        window_start=window_start,
        window_end=window_end,
        trades_1m=trades_1m,
        ob_1m=ob_1m,
        oi_1m=oi_1m,
        liq=liq,
        window_report=window_report,
        mode_id=mode_id,
    )


def _stats(vals: list[float | None]) -> dict[str, float | None]:
    clean = [float(v) for v in vals if v is not None and not (isinstance(v, float) and pd.isna(v))]
    if not clean:
        return {"median": None, "mean": None}
    s = pd.Series(clean)
    return {"median": round(float(s.median()), 6), "mean": round(float(s.mean()), 6)}


def _attach_horizons(c1m: pd.DataFrame, cands: list[dict]) -> list[dict]:
    enriched = []
    for c in cands:
        horizons = compute_all_horizons(
            c1m, direction=c["direction"], entry_at=c["entry_at"], entry_price=float(c["entry_price"])
        )
        row = {**c, **flatten_mfe_row(c, horizons)}
        for label, h in HORIZON_LABELS.items():
            oc = horizons.get(str(h)) or {}
            row[f"mfe_{label}_pct"] = oc.get("mfe_pct")
            row[f"mae_{label}_pct"] = oc.get("mae_pct")
            for tp, sl in FIRST_HIT_PAIRS:
                row[f"first_hit_{label}_t{tp:.2f}_a{sl:.2f}"] = (oc.get("first_hit_pairs") or {}).get(
                    f"t{tp:.2f}_a{sl:.2f}"
                )
            row[f"first_hit_{label}_020_020"] = (oc.get("first_hit_pairs") or {}).get("t0.20_a0.20")
        enriched.append(row)
    return enriched


def _monotonic_ok(row: dict) -> bool:
    mfes = [row.get(f"mfe_{h}_pct") for h in ("1h", "2h", "4h")]
    maes = [row.get(f"mae_{h}_pct") for h in ("1h", "2h", "4h")]
    if any(v is None or (isinstance(v, float) and pd.isna(v)) for v in mfes + maes):
        return True
    mfes = [float(x) for x in mfes]
    maes = [float(x) for x in maes]
    return mfes[2] + 1e-9 >= mfes[1] >= mfes[0] and maes[2] + 1e-9 >= maes[1] >= maes[0]


def _aggregate_table(cands: list[dict], stat: str) -> pd.DataFrame:
    rows = []
    for tf in TIMEFRAMES:
        for mode in MODE_IDS:
            pool = [c for c in cands if c["timeframe"] == tf and c["mode_id"] == mode]
            for group, fn in GROUP_MAP.items():
                sub = [c for c in pool if fn(c)]
                n = len(sub)
                flag = "NO_SAMPLE" if n == 0 else ("SMALL_SAMPLE" if n < 3 else "OK")
                row: dict[str, Any] = {
                    "signal_tf": tf,
                    "mode": mode,
                    "group": group,
                    "n": n,
                    "sample_flag": flag,
                }
                for h in ("1h", "2h", "4h"):
                    mfe_s = _stats([c.get(f"mfe_{h}_pct") for c in sub])
                    mae_s = _stats([c.get(f"mae_{h}_pct") for c in sub])
                    if stat == "median":
                        row[f"median_mfe_{h}"] = mfe_s["median"]
                        row[f"median_mae_{h}"] = mae_s["median"]
                    else:
                        row[f"mean_mfe_{h}"] = mfe_s["mean"]
                        row[f"mean_mae_{h}"] = mae_s["mean"]
                rows.append(row)
    return pd.DataFrame(rows)


def _first_hit_table(cands: list[dict]) -> pd.DataFrame:
    rows = []
    pair_key = "first_hit_{h}_020_020"
    for tf in TIMEFRAMES:
        for mode in MODE_IDS:
            pool = [c for c in cands if c["timeframe"] == tf and c["mode_id"] == mode]
            for group, fn in GROUP_MAP.items():
                sub = [c for c in pool if fn(c)]
                n = len(sub)
                for h in ("1h", "2h", "4h"):
                    hits = [c.get(pair_key.format(h=h)) for c in sub]
                    hits = [x for x in hits if x is not None and not (isinstance(x, float) and pd.isna(x))]
                    if not hits:
                        rows.append(
                            {
                                "tf": tf,
                                "mode": mode,
                                "group": group,
                                "horizon": h,
                                "n": n,
                                "sample_flag": "NO_SAMPLE" if n == 0 else ("SMALL_SAMPLE" if n < 3 else "OK"),
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
                            "sample_flag": "SMALL_SAMPLE" if n < 3 else "OK",
                            "target_first_020_020": tgt,
                            "adverse_first": adv,
                            "neither": nei,
                            "pct_target_first": round(tgt / len(hits), 6),
                        }
                    )
    return pd.DataFrame(rows)


def _candidate_readable(cands: list[dict]) -> pd.DataFrame:
    rows = []
    for c in cands:
        rows.append(
            {
                "timeframe": c["timeframe"],
                "mode_id": c["mode_id"],
                "source_group": c.get("core_research_verdict"),
                "coverage_segment": c.get("coverage_segment"),
                "direction": c["direction"],
                "candidate_at": c["candidate_at"],
                "decision_at": c["decision_at"],
                "entry_at": c["entry_at"],
                "entry_price": c["entry_price"],
                "mfe_30m_pct": c.get("mfe_30m_pct"),
                "mae_30m_pct": c.get("mae_30m_pct"),
                "mfe_1h_pct": c.get("mfe_1h_pct"),
                "mae_1h_pct": c.get("mae_1h_pct"),
                "mfe_2h_pct": c.get("mfe_2h_pct"),
                "mae_2h_pct": c.get("mae_2h_pct"),
                "mfe_4h_pct": c.get("mfe_4h_pct"),
                "mae_4h_pct": c.get("mae_4h_pct"),
                "first_hit_1h_020_020": c.get("first_hit_1h_020_020"),
                "first_hit_2h_020_020": c.get("first_hit_2h_020_020"),
                "first_hit_4h_020_020": c.get("first_hit_4h_020_020"),
                "core_research_verdict": c.get("core_research_verdict"),
                "production_gate_verdict": c.get("production_gate_verdict"),
            }
        )
    return pd.DataFrame(rows)


def _coverage_segment_table(cands: list[dict]) -> pd.DataFrame:
    rows = []
    for seg in ("CORE_INCOMPLETE", "CORE_FULL_OI_LIQ_MISSING", "CORE_FULL_OI_LIQ_PARTIAL", "FULL_MULTISOURCE"):
        sub = [c for c in cands if c.get("coverage_segment") == seg]
        rows.append({"segment": seg, "n": len(sub), "by_mode": dict(Counter(c["mode_id"] for c in sub))})
    return pd.DataFrame(rows)


def _source_filter_effect(cands: list[dict]) -> pd.DataFrame:
    rows = []
    for tf in TIMEFRAMES:
        for mode in MODE_IDS:
            pool = [c for c in cands if c["timeframe"] == tf and c["mode_id"] == mode]
            raw = pool
            sup = [c for c in pool if c.get("core_research_verdict") == "CORE_RESEARCH_SUPPORTIVE"]
            adv = [c for c in pool if c.get("core_research_verdict") == "CORE_RESEARCH_ADVERSE"]

            def med_mae(sub, h="1h"):
                s = _stats([c.get(f"mae_{h}_pct") for c in sub])["median"]
                return s

            def pct_tgt(sub, h="1h"):
                hits = [c.get(f"first_hit_{h}_020_020") for c in sub if c.get(f"first_hit_{h}_020_020")]
                return round(sum(1 for x in hits if x == "TARGET_FIRST") / len(hits), 4) if hits else None

            good_removed = sum(
                1
                for c in sup
                if c.get("mfe_1h_pct") is not None
                and c.get("mae_1h_pct") is not None
                and float(c["mfe_1h_pct"]) > float(c["mae_1h_pct"])
            )
            bad_removed = sum(
                1
                for c in adv
                if c.get("mfe_1h_pct") is not None
                and c.get("mae_1h_pct") is not None
                and float(c["mae_1h_pct"]) > float(c["mfe_1h_pct"])
            )
            rows.append(
                {
                    "timeframe": tf,
                    "mode_id": mode,
                    "n_ema_raw": len(raw),
                    "n_supportive": len(sup),
                    "n_adverse": len(adv),
                    "median_mae_1h_raw": med_mae(raw),
                    "median_mae_1h_supportive": med_mae(sup),
                    "median_mae_1h_adverse": med_mae(adv),
                    "pct_target_first_1h_raw": pct_tgt(raw),
                    "pct_target_first_1h_supportive": pct_tgt(sup),
                    "good_signals_in_adverse_cohort": good_removed,
                    "bad_signals_correctly_adverse": bad_removed,
                }
            )
    return pd.DataFrame(rows)


def _compare_previous(repo: Path, cands: list[dict], start_at: datetime, end_at: datetime) -> pd.DataFrame:
    prev_path = repo / "results/edc_sync_tolerance/xrp_mfe_mae_readable/candidate_mfe_mae_readable.csv"
    if not prev_path.exists():
        return pd.DataFrame([{"note": "previous export missing"}])
    prev = pd.read_csv(prev_path)
    prev_key = prev.apply(
        lambda r: f"{r['timeframe']}|{r['mode_id']}|{r['candidate_at']}|{r['direction']}", axis=1
    )
    prev_map = {k: prev.iloc[i].to_dict() for i, k in enumerate(prev_key)}

    rows = []
    for c in cands:
        key = f"{c['timeframe']}|{c['mode_id']}|{c['candidate_at']}|{c['direction']}"
        p = prev_map.get(key)
        if not p:
            rows.append({"key": key, "status": "NEW", "mode_id": c["mode_id"], "timeframe": c["timeframe"]})
            continue
        match_entry = str(p.get("entry_at")) == str(c.get("entry_at")) and abs(
            float(p.get("entry_price") or 0) - float(c.get("entry_price") or 0)
        ) < 1e-6
        mfe_ok = all(
            abs(float(c.get(f"mfe_{h}_pct") or 0) - float(p.get(f"mfe_{h}_pct") or 0)) < 1e-3
            for h in ("1h", "2h", "4h")
            if c.get(f"mfe_{h}_pct") is not None and p.get(f"mfe_{h}_pct") is not None
        )
        rows.append(
            {
                "key": key,
                "status": "MATCH" if match_entry and mfe_ok else "MISMATCH",
                "entry_match": match_entry,
                "mfe_mae_match": mfe_ok,
                "prev_entry_at": p.get("entry_at"),
                "new_entry_at": c.get("entry_at"),
                "prev_mfe_1h": p.get("mfe_1h_pct"),
                "new_mfe_1h": c.get("mfe_1h_pct"),
                "prev_mae_1h": p.get("mae_1h_pct"),
                "new_mae_1h": c.get("mae_1h_pct"),
            }
        )
    missing = set(prev_map) - {
        f"{c['timeframe']}|{c['mode_id']}|{c['candidate_at']}|{c['direction']}" for c in cands
    }
    for key in sorted(missing):
        rows.append({"key": key, "status": "MISSING_IN_NEW"})
    cmp_df = pd.DataFrame(rows)
    cmp_df.attrs["window_note"] = (
        f"previous implicit window Jul23-Aug22; new window {start_at.date()} to {end_at.date()}"
    )
    return cmp_df


def _daily_source_csv(preflight: dict) -> pd.DataFrame:
    ob = preflight.get("daily_ob_coverage") or []
    candles = {d["day"]: d for d in (preflight.get("daily_candle_coverage") or [])}
    rows = []
    for d in ob:
        c = candles.get(d["day"], {})
        rows.append({**d, "candle_minutes": c.get("candle_minutes"), "candle_status": c.get("status")})
    return pd.DataFrame(rows)


def _summary_md(
    preflight: dict,
    median_df: pd.DataFrame,
    mean_df: pd.DataFrame,
    cmp_df: pd.DataFrame,
    cands: list[dict],
    verdict: str,
) -> str:
    lines = [
        "# XRP 30d Core Sources Comparison",
        "",
        f"**Verdict:** `{verdict}`",
        "",
        "## A. 30-Tage-Fenster",
        "",
        f"- start_at: `{preflight['start_at']}`",
        f"- end_at: `{preflight['end_at']}` (exklusiv)",
        f"- span_days: `{preflight['span_days']}`",
        "",
        "## B. Coverage je Quelle",
        "",
    ]
    for name, rec in (preflight.get("feeds") or {}).items():
        lines.append(f"- **{name}**: status={rec.get('status')} first={rec.get('first_ts')} last={rec.get('last_ts')}")
    lines.extend(
        [
            "",
            f"- OB vollständig 30d: `{preflight.get('ob_full_30d')}` (letzter Tag `{preflight.get('daily_ob_coverage', [{}])[-1].get('day')}` oft PARTIAL)",
            f"- OB_COMPLETE_SUBWINDOW: `{preflight.get('ob_complete_subwindow')}`",
            "- Separate Tabellen: `median_table_ob_subwindow.csv`",
            "",
            "## C. Kandidaten pro TF/Modus",
            "",
        ]
    )
    for tf in TIMEFRAMES:
        for mode in MODE_IDS:
            n = sum(1 for c in cands if c["timeframe"] == tf and c["mode_id"] == mode)
            lines.append(f"- {tf} / {mode}: {n}")
    lines.extend(["", "## D. Median-Tabelle", "", "Siehe `median_table.csv`", "", "## E. Durchschnitt", "", "Siehe `mean_table.csv`"])
    if len(cmp_df):
        n_match = int((cmp_df.get("status") == "MATCH").sum()) if "status" in cmp_df else 0
        n_mis = int((cmp_df.get("status") == "MISMATCH").sum()) if "status" in cmp_df else 0
        lines.extend(["", "## J. Vergleich vorheriger Report", "", f"- MATCH: {n_match}", f"- MISMATCH: {n_mis}"])
    lines.append(f"\n**Final verdict:** `{verdict}`")
    return "\n".join(lines)


def run_xrp_30d_core_sources_comparison(
    *,
    symbol: str = "XRPUSDT",
    export_dir: str | Path | None = None,
) -> dict[str, Any]:
    symbol = str(symbol).upper()
    repo = Path(__file__).resolve().parents[4]
    out_dir = Path(export_dir) if export_dir else repo / "results/edc_sync_tolerance/xrp_30d_core_sources_comparison"
    out_dir.mkdir(parents=True, exist_ok=True)
    modes = comparison_modes()
    cfg = EMA_DUAL_CROSS_DEFAULTS

    client = default_client()
    try:
        start_at, end_at, preflight = determine_30d_window(client, symbol)
        warm_pad = timedelta(days=5)
        c1m = fetch_candles_1m(client, symbol, start_at - warm_pad, end_at + timedelta(hours=5))
        pad = timedelta(hours=2)
        trades = fetch_trades_1m(client, symbol, start_at - pad, end_at + pad)
        ob = fetch_ob_1m(client, symbol, start_at - pad, end_at + pad)
        oi = fetch_oi_1m(client, symbol, start_at - pad, end_at + pad)
        liq = fetch_liquidations(client, symbol, start_at - pad, end_at + pad)
        preflight["window_report_at_run"] = preflight.get("feeds")
    finally:
        if hasattr(client, "close"):
            client.close()

    all_cands: list[dict] = []
    for tf in TIMEFRAMES:
        df = aggregate_timeframe(c1m, tf)
        df = attach_emas(df, fast=cfg.ema_fast, medium=cfg.ema_medium, slow=cfg.ema_slow)
        df = attach_atr(df, cfg.atr_period)
        cache: dict[str, list] = {}
        for mode in modes:
            raw = detect_for_mode(mode, df, symbol=symbol, timeframe=tf, cache=cache)
            cands = evaluate_candidates_core_30d(
                raw,
                df=df,
                symbol=symbol,
                timeframe=tf,
                window_start=start_at,
                window_end=end_at,
                trades_1m=trades,
                ob_1m=ob,
                oi_1m=oi,
                liq=liq,
                window_report=preflight,
                mode_id=mode["mode_id"],
            )
            all_cands.extend(_attach_horizons(c1m, cands))

    mono_fail = [c["candidate_id"] for c in all_cands if not _monotonic_ok(c)]
    cmp_df = _compare_previous(repo, all_cands, start_at, end_at)
    mismatches = (
        int((cmp_df.get("status") == "MISMATCH").sum()) if "status" in cmp_df.columns else 0
    )

    median_df = _aggregate_table(all_cands, "median")
    mean_df = _aggregate_table(all_cands, "mean")
    first_hit_df = _first_hit_table(all_cands)
    readable_df = _candidate_readable(all_cands)
    segment_df = _coverage_segment_table(all_cands)
    effect_df = _source_filter_effect(all_cands)
    daily_df = _daily_source_csv(preflight)

    export_cands = [{k: v for k, v in c.items() if k not in ("coverage", "features")} for c in all_cands]

    verdict = "XRP_30D_CORE_SOURCES_COMPARISON_READY"
    if mono_fail:
        verdict = "XRP_30D_CORE_SOURCES_COMPARISON_FAILED"
    elif mismatches > 0:
        verdict = "XRP_30D_CORE_SOURCES_COMPARISON_PARTIAL"
    elif len(all_cands) < 10:
        verdict = "XRP_30D_CORE_SOURCES_COMPARISON_PARTIAL"

    summary = {
        "verdict": verdict,
        "n_candidates": len(all_cands),
        "window": {"start_at": start_at.isoformat(), "end_at": end_at.isoformat()},
        "monotonicity_failures": len(mono_fail),
        "previous_comparison": {
            "matches": int((cmp_df.get("status") == "MATCH").sum()) if "status" in cmp_df.columns else 0,
            "mismatches": mismatches,
            "new": int((cmp_df.get("status") == "NEW").sum()) if "status" in cmp_df.columns else 0,
        },
        "coverage_segments": dict(Counter(c.get("coverage_segment") for c in all_cands)),
        "core_research": dict(Counter(c.get("core_research_verdict") for c in all_cands)),
        "production": dict(Counter(c.get("production_gate_verdict") for c in all_cands)),
    }

    manifest = {
        "run_id": "xrp_30d_core_sources_comparison",
        "git": _git_meta(repo),
        "symbol": symbol,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "span_days": 30,
        "modes": modes,
        "timeframes": list(TIMEFRAMES),
        "core_research_policy": CORE_RESEARCH_POLICY_VERSION,
        "production_policy": POLICY_VERSION,
        "config": config_to_dict(cfg),
        "profitability_claim": False,
    }

    def wcsv(name: str, df: pd.DataFrame) -> None:
        df.to_csv(out_dir / name, index=False)

    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out_dir / "coverage_preflight.json").write_text(json.dumps(preflight, indent=2, default=str), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    wcsv("daily_source_coverage.csv", daily_df)
    wcsv("candidates_with_sources.csv", pd.DataFrame(export_cands))
    wcsv("candidate_mfe_mae_readable.csv", readable_df)
    wcsv("median_table.csv", median_df)
    wcsv("mean_table.csv", mean_df)
    wcsv("first_hit_table.csv", first_hit_df)
    wcsv("coverage_segment_table.csv", segment_df)
    wcsv("source_filter_effect.csv", effect_df)
    wcsv("previous_vs_new_comparison.csv", cmp_df)
    ob_sub = preflight.get("ob_complete_subwindow")
    if ob_sub:
        sub_start, sub_end = _utc(ob_sub["start_at"]), _utc(ob_sub["end_at"])
        sub_cands = [c for c in all_cands if sub_start <= _utc(c["candidate_at"]) < sub_end]
        wcsv("median_table_ob_subwindow.csv", _aggregate_table(sub_cands, "median"))
        wcsv("mean_table_ob_subwindow.csv", _aggregate_table(sub_cands, "mean"))
        summary["ob_subwindow"] = {"n_candidates": len(sub_cands), **ob_sub}
    (out_dir / "core_research_policy.json").write_text(
        json.dumps(core_research_policy_document(), indent=2), encoding="utf-8"
    )
    (out_dir / "summary.md").write_text(
        _summary_md(preflight, median_df, mean_df, cmp_df, all_cands, verdict), encoding="utf-8"
    )

    return {"export_dir": str(out_dir), "verdict": verdict, "summary": summary, "preflight": preflight}
