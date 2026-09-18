"""Orchestrate frozen contract → pilot → full entry-confirmation analysis."""

from __future__ import annotations

import csv
import json
import os
import resource
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.mp_edge_event_study_v1.util import ns_to_dt
from obfull_research_engine.mp_entry_confirmation_v1 import PACKAGE_NAME, RUN_ID
from obfull_research_engine.mp_entry_confirmation_v1.candles5m import (
    aggregate_5m_from_1m,
    first_allowed_5m_open_after_alert,
)
from obfull_research_engine.mp_entry_confirmation_v1.compare import (
    classify_original_vs_confirmed,
    entry_improved_vs_original,
    price_diff_pct,
    summarize_comparisons,
)
from obfull_research_engine.mp_entry_confirmation_v1.confirmation import (
    confirm_event,
    resolve_entry_1m,
)
from obfull_research_engine.mp_entry_confirmation_v1.contract import write_frozen_contract
from obfull_research_engine.mp_entry_confirmation_v1.outcomes import (
    compute_path_metrics,
    evaluate_all_pairs,
    evaluate_target_stop,
    select_entry_path,
)
from obfull_research_engine.mp_entry_confirmation_v1.params import (
    BATCH_RUN_REL,
    COST_PCT,
    DEFAULT_RUN_REL,
    ENRICH_RUN_REL,
    MAX_HOLDING_MINUTES,
    NS,
    PRICE_PATH_RUN_REL,
    STOP_PCT,
    TARGET_PCT,
    WALL_FILTER_REL,
    ConfirmParams,
)
from obfull_research_engine.mp_entry_confirmation_v1.report import (
    write_main_report,
    write_pilot_report,
    write_three_cases_report,
)
from obfull_research_engine.mp_entry_confirmation_v1.selection import (
    build_non_overlapping_confirmed_4h,
    find_manual_events,
    first_touch_ids,
    is_manual_case,
    select_pilot_events,
)
from obfull_research_engine.mp_price_path_4h_v1.candles import (
    audit_candle_source,
    load_candles_1m,
)
from obfull_research_engine.mp_price_path_4h_v1.wall_filter import (
    event_passes_wall_persistence,
    load_frozen_wall_filter,
)


def _peak_rss_mib() -> float:
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen and "bps" not in k.lower():
                seen.add(k)
                keys.append(k)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})
    os.replace(tmp, path)


def _to_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        pd.DataFrame().to_parquet(path, index=False)
        return
    # drop nested / heavy sequence fields from wide tables when present as lists of dicts
    clean = []
    for r in rows:
        c = {k: v for k, v in r.items() if not (isinstance(v, (list, dict)) and k.endswith("_events"))}
        # path_rows stored separately
        c.pop("path_rows", None)
        c.pop("sequence_events", None)
        clean.append(c)
    pd.DataFrame(clean).to_parquet(path, index=False)


def _fmt_utc(ns: int | None) -> str | None:
    if ns is None:
        return None
    return ns_to_dt(int(ns)).strftime("%Y-%m-%d %H:%M:%S UTC")


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def _read_events(batch_dir: Path) -> list[dict[str, Any]]:
    import pandas as pd

    return pd.read_parquet(batch_dir / "events_all.parquet").to_dict(orient="records")


def _read_features(enrich_dir: Path) -> dict[str, dict[str, Any]]:
    import pandas as pd

    df = pd.read_parquet(enrich_dir / "event_features.parquet")
    return {str(r["event_id"]): r for r in df.to_dict(orient="records")}


def _read_selection(batch_dir: Path) -> list[dict[str, Any]]:
    import pandas as pd

    return pd.read_parquet(batch_dir / "selection_policies.parquet").to_dict(orient="records")


def _read_complete_4h_ids(price_dir: Path) -> set[str]:
    import pandas as pd

    hz = pd.read_parquet(price_dir / "event_horizon_excursions.parquet")
    sub = hz[(hz["horizon_min"] == 240) & (hz["is_complete"] == True)]  # noqa: E712
    return set(sub["event_id"].astype(str))


def audit_inputs(
    events: list[dict[str, Any]],
    features: dict[str, dict[str, Any]],
    complete_4h: set[str],
    *,
    batch_dir: Path,
    enrich_dir: Path,
    price_dir: Path,
) -> dict[str, Any]:
    tradable = [
        e
        for e in events
        if e.get("label_price_only") in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK")
        and e.get("trade_side") in ("LONG", "SHORT")
        and not _truthy(e.get("is_censored", False))
    ]
    tradable_complete = [e for e in tradable if str(e["event_id"]) in complete_4h]
    return {
        "ok": len(tradable_complete) == 251 and len(events) == 282,
        "agents_md_found": False,
        "agents_md_note": "No AGENTS.md under orderbook_analyse_ch_research_v1; read README/REQUIREMENT_MATRIX and input run manifests instead.",
        "n_events": len(events),
        "tradable_labeled": len(tradable),
        "tradable_complete_4h": len(tradable_complete),
        "feature_rows": len(features),
        "batch_dir": str(batch_dir),
        "enrich_dir": str(enrich_dir),
        "price_path_dir": str(price_dir),
        "expected_tradable_complete_4h": 251,
    }


def net_expectancy(results: list[dict[str, Any]], *, cost_pct: float) -> dict[str, Any]:
    """Expectancy over confirmed trades using conservative ambiguous=stop."""
    pnls = []
    for r in results:
        res = r.get("conservative_result") or r.get("primary_result")
        if res == "TARGET_FIRST":
            pnls.append(TARGET_PCT - cost_pct)
        elif res == "STOP_FIRST":
            pnls.append(-(STOP_PCT + cost_pct))
        elif res == "NEITHER":
            cr = r.get("horizon_close_return_pct")
            if cr is None:
                continue
            pnls.append(float(cr) - cost_pct)
        elif res == "AMBIGUOUS":
            pnls.append(-(STOP_PCT + cost_pct))
        # CENSORED / NO entry skipped
    if not pnls:
        return {"n": 0, "expectancy_pct": None, "cost_pct": cost_pct}
    return {
        "n": len(pnls),
        "expectancy_pct": sum(pnls) / len(pnls),
        "cost_pct": cost_pct,
        "sum_pnl_pct": sum(pnls),
    }


def analyze_one_event(
    event: dict[str, Any],
    *,
    candles_1m: list[Any],
    candles_5m: list[Any],
    wall: bool,
    first_touch: bool,
) -> dict[str, Any]:
    eid = str(event["event_id"])
    alert_ts = int(event["trigger_ts_ns"])
    label = str(event["label_price_only"])
    side = str(event["trade_side"])
    clow = float(event["confluence_low"])
    chigh = float(event["confluence_high"])
    orig_px = float(event["trigger_price"])

    conf = confirm_event(
        candles_5m,
        alert_ts_ns=alert_ts,
        label=label,
        trade_side=side,
        confluence_low=clow,
        confluence_high=chigh,
    )

    base = {
        "event_id": eid,
        "label_price_only": label,
        "trade_side": side,
        "event_role": str(event.get("event_role")),
        "confluence_class": str(event.get("confluence_class")),
        "window_id": str(event.get("window_id")),
        "alert_ts_ns": alert_ts,
        "alert_ts_utc": _fmt_utc(alert_ts),
        "original_trigger_price": orig_px,
        "confluence_low": clow,
        "confluence_high": chigh,
        "wall_persistence": bool(wall),
        "first_touch_policy": bool(first_touch),
        "confirmation_reason": conf.reason,
        "confirmation_type": conf.confirmation_type,
        "confirmed": bool(conf.confirmed),
        "confirmation_ts_ns": conf.confirmation_ts,
        "confirmation_ts_utc": _fmt_utc(conf.confirmation_ts),
        "confirmation_details": conf.details,
        "sequence_events": conf.sequence_events,
        "confirmation_reset_count": (conf.details or {}).get("confirmation_reset_count"),
        "confirmation_delay_minutes": (conf.details or {}).get("confirmation_delay_minutes"),
    }

    # Original entry outcome (from alert as entry open minute path — use trigger bar open path)
    orig_path = select_entry_path(candles_1m, entry_ts_ns=alert_ts, horizon_min=MAX_HOLDING_MINUTES)
    if orig_path is None or len(orig_path) < MAX_HOLDING_MINUTES:
        # try floor minute of alert
        from obfull_research_engine.mp_price_path_4h_v1.candles import floor_minute_ns

        orig_path = select_entry_path(
            candles_1m, entry_ts_ns=floor_minute_ns(alert_ts), horizon_min=MAX_HOLDING_MINUTES
        )
    if orig_path and len(orig_path) >= MAX_HOLDING_MINUTES:
        orig_eval = evaluate_target_stop(
            orig_path, trade_side=side, entry_price=orig_px, target_pct=TARGET_PCT, stop_pct=STOP_PCT
        )
        orig_metrics = compute_path_metrics(orig_path, trade_side=side, entry_price=orig_px)
    else:
        orig_eval = {"result": "CENSORED", "conservative_result": "CENSORED"}
        orig_metrics = {}

    base["original_primary_result"] = orig_eval.get("result")
    base["original_conservative_result"] = orig_eval.get("conservative_result")
    base["original_mae_before_0_41_pct"] = orig_metrics.get("mae_before_0_41_pct")
    base["original_minutes_to_0_41_pct"] = orig_metrics.get("minutes_to_0_41_pct")

    if not conf.confirmed or conf.confirmation_ts is None:
        base["no_entry_reason"] = conf.reason
        base["entry_ts_ns"] = None
        base["entry_price"] = None
        base["primary_result"] = "NO_CONFIRMED_ENTRY"
        base["conservative_result"] = "NO_CONFIRMED_ENTRY"
        base["comparison_class"] = classify_original_vs_confirmed(
            original_result=base["original_primary_result"],
            confirmed_result=None,
            confirmed_entry=False,
        )
        return base

    entry_ts, entry_px, err = resolve_entry_1m(candles_1m, confirmation_ts_ns=conf.confirmation_ts)
    if err or entry_ts is None or entry_px is None:
        base["no_entry_reason"] = err or "MISSING_ENTRY_CANDLE"
        base["confirmed"] = False
        base["primary_result"] = "NO_CONFIRMED_ENTRY"
        base["conservative_result"] = "NO_CONFIRMED_ENTRY"
        base["comparison_class"] = classify_original_vs_confirmed(
            original_result=base["original_primary_result"],
            confirmed_result=None,
            confirmed_entry=False,
        )
        return base

    path = select_entry_path(candles_1m, entry_ts_ns=entry_ts, horizon_min=MAX_HOLDING_MINUTES)
    if path is None:
        base["no_entry_reason"] = "MISSING_ENTRY_CANDLE"
        base["confirmed"] = False
        base["primary_result"] = "NO_CONFIRMED_ENTRY"
        base["conservative_result"] = "NO_CONFIRMED_ENTRY"
        base["comparison_class"] = classify_original_vs_confirmed(
            original_result=base["original_primary_result"],
            confirmed_result=None,
            confirmed_entry=False,
        )
        return base

    pairs = evaluate_all_pairs(path, trade_side=side, entry_price=entry_px)
    primary = pairs["primary"]
    metrics = compute_path_metrics(path, trade_side=side, entry_price=entry_px)

    alert_to_entry_min = (entry_ts - alert_ts) / (60 * NS)
    improved = entry_improved_vs_original(
        trade_side=side, original_price=orig_px, confirmed_price=entry_px
    )
    diff = price_diff_pct(trade_side=side, original_price=orig_px, confirmed_price=entry_px)

    base.update(
        {
            "no_entry_reason": None,
            "entry_ts_ns": entry_ts,
            "entry_ts_utc": _fmt_utc(entry_ts),
            "entry_price": entry_px,
            "confirmed_entry_price": entry_px,
            "entry_price_difference_pct": diff,
            "entry_improved_vs_original": improved,
            "alert_to_entry_minutes": alert_to_entry_min,
            "primary_result": primary.get("result"),
            "conservative_result": primary.get("conservative_result"),
            "minutes_to_target": primary.get("minutes_to_target"),
            "minutes_to_stop": primary.get("minutes_to_stop"),
            "horizon_close_return_pct": primary.get("close_return_pct")
            or metrics.get("close_return_240m_pct"),
            "qualified_move": primary.get("result") == "TARGET_FIRST",
            "diagnostic_results": pairs["diagnostic"],
            "path_rows": metrics.get("path_rows"),
            "comparison_class": classify_original_vs_confirmed(
                original_result=base["original_primary_result"],
                confirmed_result=primary.get("result"),
                confirmed_entry=True,
            ),
        }
    )
    for k, v in metrics.items():
        if k == "path_rows":
            continue
        base[k] = v
    # flatten details of interest
    for k, v in (conf.details or {}).items():
        if k not in base:
            base[k] = v
    return base


def _median(vals: list[float]) -> float | None:
    xs = sorted(v for v in vals if v is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return 0.5 * (xs[mid - 1] + xs[mid])


def _groupby_rate(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    buckets: dict[Any, list] = defaultdict(list)
    for r in rows:
        buckets[r.get(key)].append(r)
    out = []
    for k, rs in sorted(buckets.items(), key=lambda x: str(x[0])):
        conf = [r for r in rs if r.get("confirmed")]
        q = sum(1 for r in conf if r.get("primary_result") == "TARGET_FIRST")
        out.append(
            {
                key: k,
                "n_events": len(rs),
                "n_confirmed": len(conf),
                "n_target_first": q,
                "qualified_move_rate": (q / len(conf) if conf else None),
                "n_no_entry": sum(1 for r in rs if not r.get("confirmed")),
            }
        )
    return out


def run_entry_confirmation(
    *,
    repo_root: Path,
    params: ConfirmParams | None = None,
    client: Any | None = None,
    pilot_only: bool = False,
) -> dict[str, Any]:
    repo_root = Path(repo_root)
    params = params or ConfirmParams(
        batch_run_dir=repo_root / BATCH_RUN_REL,
        enrich_run_dir=repo_root / ENRICH_RUN_REL,
        price_path_run_dir=repo_root / PRICE_PATH_RUN_REL,
        out_dir=repo_root / DEFAULT_RUN_REL,
    )
    out_dir = Path(params.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "confirmation.log"
    t0 = time.time()

    def log(msg: str) -> None:
        line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)

    log(f"START {PACKAGE_NAME} {RUN_ID}")

    # 1) Freeze contract BEFORE outcomes
    contract = write_frozen_contract(out_dir / "frozen_entry_confirmation_contract_v1.json")
    log(f"CONTRACT_HASH {contract['contract_hash_sha256']}")

    events = _read_events(params.batch_run_dir)
    features = _read_features(params.enrich_run_dir)
    selection = _read_selection(params.batch_run_dir)
    complete_4h = _read_complete_4h_ids(params.price_path_run_dir)
    ft_ids = first_touch_ids(selection)
    wall_frozen = load_frozen_wall_filter(repo_root / WALL_FILTER_REL)
    wall_flags = {
        eid: event_passes_wall_persistence(feat, wall_frozen) for eid, feat in features.items()
    }

    input_audit = audit_inputs(
        events,
        features,
        complete_4h,
        batch_dir=params.batch_run_dir,
        enrich_dir=params.enrich_run_dir,
        price_dir=params.price_path_run_dir,
    )
    _write_json(out_dir / "input_audit.json", input_audit)
    if not input_audit["ok"]:
        verdict = "ENTRY_CONFIRMATION_BLOCKED_INPUT"
        write_main_report(out_dir / "ENTRY_CONFIRMATION_REPORT.md", verdict=verdict, summary=input_audit)
        return {"verdict": verdict, "input_audit": input_audit}

    if client is None:
        from obfull_research_engine.clickhouse_research_store_v1.helpers import (
            get_clickhouse_client,
        )

        client = get_clickhouse_client(role="mp_entry_confirmation_v1_read")

    candle_audit = audit_candle_source(client, symbol=params.symbol)
    if not candle_audit.get("ok"):
        verdict = "ENTRY_CONFIRMATION_BLOCKED_CANDLE_DATA"
        write_main_report(
            out_dir / "ENTRY_CONFIRMATION_REPORT.md",
            verdict=verdict,
            summary={"candle_audit": candle_audit},
        )
        return {"verdict": verdict, "candle_audit": candle_audit}

    # Load candle span covering all alerts + timeout + holding + buffer
    tradable = [
        e
        for e in events
        if e.get("label_price_only") in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK")
        and e.get("trade_side") in ("LONG", "SHORT")
        and not _truthy(e.get("is_censored", False))
        and str(e["event_id"]) in complete_4h
    ]
    tmin = min(int(e["trigger_ts_ns"]) for e in tradable)
    tmax = max(int(e["trigger_ts_ns"]) for e in tradable)
    # buffer: 6h before for structure lookback context + 120m confirm + 240m hold + 1h
    start = ns_to_dt(tmin - 6 * 3600 * NS)
    end = ns_to_dt(tmax + (120 + 240 + 60) * 60 * NS)
    log(f"LOAD_CANDLES {start} -> {end}")
    candles_1m = load_candles_1m(client, start=start, end=end, symbol=params.symbol)
    candles_5m = aggregate_5m_from_1m(candles_1m)
    log(f"CANDLES_1M={len(candles_1m)} CANDLES_5M={len(candles_5m)}")

    # --- PILOT ---
    pilot_events = select_pilot_events(tradable, wall_flags)
    _write_csv(
        out_dir / "pilot_events.csv",
        [
            {
                "event_id": e["event_id"],
                "label": e["label_price_only"],
                "trade_side": e["trade_side"],
                "wall": wall_flags.get(str(e["event_id"]), False),
                "manual": is_manual_case(e),
                "trigger_ts_utc": _fmt_utc(int(e["trigger_ts_ns"])),
            }
            for e in pilot_events
        ],
    )
    pilot_rows = []
    for e in pilot_events:
        eid = str(e["event_id"])
        row = analyze_one_event(
            e,
            candles_1m=candles_1m,
            candles_5m=candles_5m,
            wall=wall_flags.get(eid, False),
            first_touch=eid in ft_ids,
        )
        pilot_rows.append(row)

    manuals = find_manual_events(tradable)
    three_cases = []
    for i, e in enumerate(manuals, 1):
        eid = str(e["event_id"])
        row = next(r for r in pilot_rows if r["event_id"] == eid)
        notes = []
        if i == 1:
            notes.append("CLEAN FAILED_BREAK plausibility: check confirmed entry timing vs original.")
        elif i == 2:
            notes.append("ABSORB: check sequence resets and later reclaim entry / MAE.")
        else:
            notes.append("TRUE_BREAK: check retest rejection / later continuation entry / MAE.")
        three_cases.append(
            {
                "case_id": f"CASE_{i}",
                "event_id": eid,
                "label": row["label_price_only"],
                "trade_side": row["trade_side"],
                "alert_ts_utc": row["alert_ts_utc"],
                "confirmed": row["confirmed"],
                "no_entry_reason": row.get("no_entry_reason"),
                "confirmation_ts_utc": row.get("confirmation_ts_utc"),
                "entry_ts_utc": row.get("entry_ts_utc"),
                "entry_price": row.get("entry_price"),
                "alert_to_entry_minutes": row.get("alert_to_entry_minutes"),
                "entry_price_difference_pct": row.get("entry_price_difference_pct"),
                "entry_improved_vs_original": row.get("entry_improved_vs_original"),
                "primary_result": row.get("primary_result"),
                "mae_before_0_41_pct": row.get("mae_before_0_41_pct"),
                "sequence_reset_count": row.get("sequence_reset_count")
                or row.get("confirmation_reset_count"),
                "confirmation_details": row.get("confirmation_details"),
                "notes": " ".join(notes),
            }
        )
    _write_csv(out_dir / "three_manual_cases.csv", three_cases)
    write_three_cases_report(out_dir / "THREE_CASES_REPORT.md", three_cases)

    pilot_checks = {
        "n_pilot": len(pilot_rows),
        "n_manual": len(manuals),
        "first_allowed_5m_helper_ok": first_allowed_5m_open_after_alert(1_725_987_857 * NS)
        % (5 * 60 * NS)
        == 0,
        "contract_hash": contract["contract_hash_sha256"],
        "units_percent": True,
        "no_mp_recompute": True,
        "no_ob_recompute": True,
    }
    pilot_ok = len(pilot_rows) <= 20 and len(manuals) == 3
    write_pilot_report(
        out_dir / "PILOT_REPORT.md",
        summary={"pilot_ok": pilot_ok, "checks": pilot_checks},
        cases=three_cases,
    )
    log(f"PILOT_DONE n={len(pilot_rows)} ok={pilot_ok}")
    if pilot_only:
        return {"verdict": "PILOT_ONLY", "pilot_ok": pilot_ok, "three_cases": three_cases}

    if not pilot_ok:
        verdict = "ENTRY_CONFIRMATION_BLOCKED_IMPLEMENTATION"
        write_main_report(
            out_dir / "ENTRY_CONFIRMATION_REPORT.md",
            verdict=verdict,
            summary={"pilot_checks": pilot_checks},
        )
        return {"verdict": verdict}

    # --- FULL ---
    all_rows = []
    for i, e in enumerate(tradable):
        eid = str(e["event_id"])
        row = analyze_one_event(
            e,
            candles_1m=candles_1m,
            candles_5m=candles_5m,
            wall=wall_flags.get(eid, False),
            first_touch=eid in ft_ids,
        )
        all_rows.append(row)
        if (i + 1) % 50 == 0:
            log(f"PROGRESS {i+1}/{len(tradable)}")

    confirmed = [r for r in all_rows if r.get("confirmed")]
    no_entry = [r for r in all_rows if not r.get("confirmed")]
    seq_rows = []
    for r in all_rows:
        for ev in r.get("sequence_events") or []:
            seq_rows.append(
                {
                    "event_id": r["event_id"],
                    "label_price_only": r["label_price_only"],
                    "trade_side": r["trade_side"],
                    **ev,
                }
            )

    path_rows = []
    for r in confirmed:
        for pr in r.get("path_rows") or []:
            path_rows.append({"event_id": r["event_id"], **pr})

    target_stop_rows = []
    for r in confirmed:
        target_stop_rows.append(
            {
                "event_id": r["event_id"],
                "label_price_only": r["label_price_only"],
                "trade_side": r["trade_side"],
                "wall_persistence": r["wall_persistence"],
                "primary_result": r.get("primary_result"),
                "conservative_result": r.get("conservative_result"),
                "minutes_to_target": r.get("minutes_to_target"),
                "minutes_to_stop": r.get("minutes_to_stop"),
                "mfe_240m_pct": r.get("mfe_240m_pct"),
                "mae_240m_pct": r.get("mae_240m_pct"),
                "mae_before_0_41_pct": r.get("mae_before_0_41_pct"),
                "horizon_close_return_pct": r.get("horizon_close_return_pct"),
            }
        )

    compare_rows = [
        {
            "event_id": r["event_id"],
            "label_price_only": r["label_price_only"],
            "trade_side": r["trade_side"],
            "wall_persistence": r["wall_persistence"],
            "first_touch_policy": r["first_touch_policy"],
            "confirmed": r["confirmed"],
            "original_primary_result": r.get("original_primary_result"),
            "confirmed_primary_result": r.get("primary_result") if r["confirmed"] else None,
            "comparison_class": r.get("comparison_class"),
            "entry_improved_vs_original": r.get("entry_improved_vs_original"),
            "alert_to_entry_minutes": r.get("alert_to_entry_minutes"),
        }
        for r in all_rows
    ]
    comp_summary = summarize_comparisons(compare_rows)

    non_overlap = build_non_overlapping_confirmed_4h(confirmed)

    _to_parquet(out_dir / "confirmed_entries.parquet", confirmed)
    _to_parquet(out_dir / "no_entry_events.parquet", no_entry)
    _to_parquet(out_dir / "confirmation_sequences.parquet", seq_rows)
    _to_parquet(out_dir / "original_vs_confirmed.parquet", compare_rows)
    _to_parquet(out_dir / "confirmed_price_paths.parquet", path_rows)
    _to_parquet(out_dir / "target_stop_results.parquet", target_stop_rows)
    _to_parquet(out_dir / "non_overlapping_confirmed_4h.parquet", non_overlap)

    _write_csv(out_dir / "summary_by_label.csv", _groupby_rate(all_rows, "label_price_only"))
    _write_csv(out_dir / "summary_by_trade_side.csv", _groupby_rate(all_rows, "trade_side"))
    _write_csv(
        out_dir / "summary_by_wall_persistence.csv", _groupby_rate(all_rows, "wall_persistence")
    )
    _write_csv(out_dir / "summary_by_confluence.csv", _groupby_rate(all_rows, "confluence_class"))
    _write_csv(
        out_dir / "original_vs_confirmed_summary.csv",
        [{"comparison_class": k, "n": v} for k, v in sorted(comp_summary.items())],
    )

    # First-touch subset
    ft_rows = [r for r in all_rows if r.get("first_touch_policy")]
    ft_conf = [r for r in ft_rows if r.get("confirmed")]

    def count_res(rows: list[dict[str, Any]], res: str) -> int:
        return sum(1 for r in rows if r.get("primary_result") == res)

    conf_only = confirmed
    n_target = count_res(conf_only, "TARGET_FIRST")
    n_stop = count_res(conf_only, "STOP_FIRST")
    n_neither = count_res(conf_only, "NEITHER")
    n_amb = count_res(conf_only, "AMBIGUOUS")
    qrate = (n_target / len(conf_only)) if conf_only else None

    orig_q = sum(1 for r in all_rows if r.get("original_primary_result") == "TARGET_FIRST")
    orig_qrate = orig_q / len(all_rows) if all_rows else None

    exp08 = net_expectancy(conf_only, cost_pct=COST_PCT[0])
    exp12 = net_expectancy(conf_only, cost_pct=COST_PCT[1])

    wall_true = [r for r in conf_only if r.get("wall_persistence")]
    wall_false = [r for r in conf_only if not r.get("wall_persistence")]
    wall_cmp = {
        "wall_true_n": len(wall_true),
        "wall_true_qrate": (
            sum(1 for r in wall_true if r.get("primary_result") == "TARGET_FIRST") / len(wall_true)
            if wall_true
            else None
        ),
        "wall_false_n": len(wall_false),
        "wall_false_qrate": (
            sum(1 for r in wall_false if r.get("primary_result") == "TARGET_FIRST")
            / len(wall_false)
            if wall_false
            else None
        ),
    }

    median_alert_to_entry = _median(
        [float(r["alert_to_entry_minutes"]) for r in conf_only if r.get("alert_to_entry_minutes") is not None]
    )
    median_mfe = _median([float(r["mfe_240m_pct"]) for r in conf_only if r.get("mfe_240m_pct") is not None])
    median_mae = _median([float(r["mae_240m_pct"]) for r in conf_only if r.get("mae_240m_pct") is not None])
    median_mae_before = _median(
        [float(r["mae_before_0_41_pct"]) for r in conf_only if r.get("mae_before_0_41_pct") is not None]
    )

    runtime_s = time.time() - t0
    peak = _peak_rss_mib()
    runtime = {
        "runtime_seconds": runtime_s,
        "peak_rss_mib": peak,
        "n_events_analyzed": len(all_rows),
        "n_confirmed": len(conf_only),
    }
    _write_json(out_dir / "runtime_metrics.json", runtime)

    # Verdict
    if len(conf_only) == 0:
        verdict = "ENTRY_CONFIRMATION_NO_EDGE"
    elif len(conf_only) < 30:
        verdict = "ENTRY_CONFIRMATION_SUCCESS_LOW_SAMPLE"
    elif qrate is not None and qrate <= max(0.0, (orig_qrate or 0) - 0.02) and comp_summary.get(
        "MISSED_WINNER", 0
    ) > comp_summary.get("AVOIDED_BAD_TRADE", 0) + comp_summary.get("IMPROVED", 0):
        verdict = "ENTRY_CONFIRMATION_NO_EDGE"
    else:
        verdict = "ENTRY_CONFIRMATION_SUCCESS"

    summary = {
        "verdict": verdict,
        "input_events": len(events),
        "tradable_complete_4h": len(all_rows),
        "first_touch_events": len(ft_rows),
        "first_touch_confirmed": len(ft_conf),
        "confirmed_entries": len(conf_only),
        "no_confirmed_entry": len(no_entry),
        "confirmed_by_label": {
            lab: sum(1 for r in conf_only if r["label_price_only"] == lab)
            for lab in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK")
        },
        "confirmed_long": sum(1 for r in conf_only if r["trade_side"] == "LONG"),
        "confirmed_short": sum(1 for r in conf_only if r["trade_side"] == "SHORT"),
        "median_alert_to_entry_minutes": median_alert_to_entry,
        "target_041_first": n_target,
        "stop_015_first": n_stop,
        "neither": n_neither,
        "ambiguous": n_amb,
        "qualified_move_rate_confirmed": qrate,
        "original_qualified_move_rate": orig_qrate,
        "comparison": comp_summary,
        "median_mfe_4h_pct": median_mfe,
        "median_mae_4h_pct": median_mae,
        "median_mae_before_041_pct": median_mae_before,
        "net_expectancy_0_08": exp08,
        "net_expectancy_0_12": exp12,
        "wall_persistence_vs_base": wall_cmp,
        "non_overlapping_confirmed_4h": len(non_overlap),
        "three_manual_cases": three_cases,
        "contract_hash": contract["contract_hash_sha256"],
        "runtime": runtime,
        "candle_source": candle_audit.get("full_name"),
    }
    _write_json(
        out_dir / "run_manifest.json",
        {
            "package": PACKAGE_NAME,
            "run_id": RUN_ID,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "params": params.to_dict(),
            "contract_hash": contract["contract_hash_sha256"],
            "verdict": verdict,
            "summary": summary,
            "notes": [
                "MP batch / OB enrichment / prior price-path runs read-only",
                "No parameter retuning after outcomes",
                "ClickHouse candles read-only",
            ],
        },
    )
    write_main_report(out_dir / "ENTRY_CONFIRMATION_REPORT.md", verdict=verdict, summary=summary)
    log(f"DONE verdict={verdict} confirmed={len(conf_only)} runtime_s={runtime_s:.1f}")
    return {"verdict": verdict, "summary": summary, "out_dir": str(out_dir)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="mp_entry_confirmation_v1")
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--pilot-only", action="store_true")
    p.add_argument("--out-dir", type=Path, default=None)
    args = p.parse_args(argv)
    params = None
    if args.out_dir is not None:
        root = Path(args.repo_root)
        params = ConfirmParams(
            batch_run_dir=root / BATCH_RUN_REL,
            enrich_run_dir=root / ENRICH_RUN_REL,
            price_path_run_dir=root / PRICE_PATH_RUN_REL,
            out_dir=Path(args.out_dir),
        )
    try:
        run_entry_confirmation(repo_root=Path(args.repo_root), params=params, pilot_only=args.pilot_only)
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
