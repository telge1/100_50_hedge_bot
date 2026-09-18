"""Orchestrate candle audit → pilot → full 4h price-path analysis."""

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
from obfull_research_engine.mp_price_path_4h_v1 import PACKAGE_NAME, RUN_ID
from obfull_research_engine.mp_price_path_4h_v1.candles import (
    audit_candle_source,
    load_candles_1m,
)
from obfull_research_engine.mp_price_path_4h_v1.params import (
    BATCH_RUN_REL,
    COST_PCT,
    DEFAULT_RUN_REL,
    ENRICH_RUN_REL,
    FIRST_HIT_HORIZONS_MIN,
    HORIZONS_MIN,
    MFE_TARGETS_PCT,
    NET_TARGETS_PCT,
    NS,
    PILOT_MAX_EVENTS,
    PathParams,
)
from obfull_research_engine.mp_price_path_4h_v1.path_engine import analyze_event_path
from obfull_research_engine.mp_price_path_4h_v1.report import write_report
from obfull_research_engine.mp_price_path_4h_v1.selection import (
    build_non_overlapping_4h,
    policy_ids,
)
from obfull_research_engine.mp_price_path_4h_v1.summaries import (
    sample_flag,
    summarize_first_hit,
    summarize_horizons,
    summarize_targets,
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
            if k not in seen:
                seen.add(k)
                keys.append(k)
    # reject bps columns in user outputs
    keys = [k for k in keys if "bps" not in k.lower()]
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})
    os.replace(tmp, path)


def _read_events(batch_dir: Path) -> list[dict[str, Any]]:
    import pandas as pd

    df = pd.read_parquet(batch_dir / "events_all.parquet")
    return df.to_dict(orient="records")


def _read_episodes(batch_dir: Path) -> list[dict[str, Any]]:
    import pandas as pd

    return pd.read_parquet(batch_dir / "episodes.parquet").to_dict(orient="records")


def _read_features(enrich_dir: Path) -> dict[str, dict[str, Any]]:
    import pandas as pd

    df = pd.read_parquet(enrich_dir / "event_features.parquet")
    return {str(r["event_id"]): r for r in df.to_dict(orient="records")}


def audit_event_inputs(
    events: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    features: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    n = len(events)
    missing_trig = 0
    missing_price = 0
    missing_side = 0
    tradable = 0
    for e in events:
        lab = e.get("label_price_only")
        is_tradable_label = lab in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK")
        trig = e.get("trigger_ts_ns")
        price = e.get("trigger_price")
        side = e.get("trade_side")
        trig_missing = trig in (None, "", "None") or (
            isinstance(trig, float) and trig != trig
        )
        price_missing = price in (None, "", "None") or (
            isinstance(price, float) and price != price
        )
        side_missing = side not in ("LONG", "SHORT")
        if is_tradable_label:
            tradable += 1
            if trig_missing:
                missing_trig += 1
            if price_missing:
                missing_price += 1
            if side_missing:
                missing_side += 1
    return {
        "ok": n == 282 and len({e["event_id"] for e in events}) == 282 and missing_trig == 0 and missing_price == 0 and missing_side == 0,
        "n_events": n,
        "unique_event_ids": len({e["event_id"] for e in events}),
        "tradable_labeled_events": tradable,
        "missing_trigger_ts_among_tradable": missing_trig,
        "missing_trigger_price_among_tradable": missing_price,
        "missing_trade_side_among_tradable": missing_side,
        "unresolved_or_non_tradable": n - tradable,
        "n_episodes": len(episodes),
        "n_feature_rows": len(features),
        "feature_coverage": sum(1 for e in events if e["event_id"] in features),
    }


def select_pilot(events: list[dict[str, Any]], wall_flags: dict[str, bool], *, max_n: int) -> list[dict[str, Any]]:
    by_key: dict[tuple, list] = defaultdict(list)
    for e in events:
        if e.get("trade_side") not in ("LONG", "SHORT"):
            continue
        if e.get("trigger_price") in (None, "", "None"):
            continue
        key = (
            e.get("window_id"),
            e.get("label_price_only"),
            e.get("trade_side"),
            bool(wall_flags.get(e["event_id"], False)),
        )
        by_key[key].append(e)
    selected: list[dict[str, Any]] = []
    keys = sorted(by_key.keys(), key=lambda x: str(x))
    while len(selected) < max_n and keys:
        progressed = False
        for k in list(keys):
            if not by_key[k]:
                keys.remove(k)
                continue
            selected.append(by_key[k].pop(0))
            progressed = True
            if len(selected) >= max_n:
                break
        if not progressed:
            break
    return selected[:max_n]


def run_price_path(
    *,
    repo_root: Path,
    params: PathParams | None = None,
    client: Any | None = None,
    pilot_only: bool = False,
) -> dict[str, Any]:
    repo_root = Path(repo_root)
    params = params or PathParams(
        batch_run_dir=repo_root / BATCH_RUN_REL,
        enrich_run_dir=repo_root / ENRICH_RUN_REL,
        out_dir=repo_root / DEFAULT_RUN_REL,
    )
    out_dir = Path(params.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "price_path.log"
    t0 = time.time()

    def log(msg: str) -> None:
        line = f"{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')} {msg}"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    log(f"start {PACKAGE_NAME}")
    # frozen wall filter: prefer coverage-audit path if present, else local freeze
    cov = repo_root / "obfull_research_engine/runs/mp_coverage_recovery_audit_v1_20260916/frozen_wall_persistence_filter_v1.json"
    local_freeze = out_dir / "frozen_wall_persistence_filter_v1.json"
    if cov.exists():
        frozen = load_frozen_wall_filter(cov)
        _write_json(local_freeze, frozen)
    elif local_freeze.exists():
        frozen = load_frozen_wall_filter(local_freeze)
    else:
        raise RuntimeError("FROZEN_WALL_FILTER_MISSING")

    events = _read_events(params.batch_run_dir)
    # normalize records
    for e in events:
        e["event_id"] = str(e["event_id"])
        if e.get("trigger_ts_ns") is not None and str(e.get("trigger_ts_ns")) not in ("", "None", "nan"):
            e["trigger_ts_ns"] = int(e["trigger_ts_ns"])
        else:
            e["trigger_ts_ns"] = None
        if e.get("trigger_price") is not None and str(e.get("trigger_price")) not in ("", "None", "nan"):
            e["trigger_price"] = float(e["trigger_price"])
        else:
            e["trigger_price"] = None
        if e.get("trade_side") is not None:
            e["trade_side"] = str(e["trade_side"]) if str(e["trade_side"]) not in ("", "None", "nan") else None

    episodes = _read_episodes(params.batch_run_dir)
    for ep in episodes:
        ep["event_id"] = str(ep["event_id"])
    features = _read_features(params.enrich_run_dir)
    input_audit = audit_event_inputs(events, episodes, features)
    _write_json(out_dir / "event_input_audit.json", input_audit)
    if not input_audit.get("ok"):
        verdict = "PRICE_PATH_4H_BLOCKED_INPUT"
        _write_json(out_dir / "run_manifest.json", {"verdict": verdict, "input_audit": input_audit})
        write_report(out_dir, verdict=verdict, summary={"input_audit": input_audit})
        return {"verdict": verdict, "out_dir": str(out_dir)}

    own_client = False
    if client is None:
        from obfull_research_engine.clickhouse_research_store_v1.helpers import (
            get_clickhouse_client,
        )

        client = get_clickhouse_client(role="mp_price_path_4h_read")
        own_client = True

    try:
        candle_audit = audit_candle_source(client, symbol=params.symbol)
        _write_json(out_dir / "candle_source_audit.json", candle_audit)
        if not candle_audit.get("ok"):
            verdict = "PRICE_PATH_4H_BLOCKED_CANDLE_DATA"
            _write_json(
                out_dir / "run_manifest.json",
                {"verdict": verdict, "candle_audit": candle_audit},
            )
            write_report(out_dir, verdict=verdict, summary={"candle_audit": candle_audit})
            return {"verdict": verdict, "out_dir": str(out_dir)}

        wall_flags = {
            eid: event_passes_wall_persistence(feat, frozen)
            for eid, feat in features.items()
        }
        # ensure all events have flag
        for e in events:
            wall_flags.setdefault(e["event_id"], False)

        non_ov = build_non_overlapping_4h(events)
        try:
            import pandas as pd

            pd.DataFrame(non_ov).to_parquet(out_dir / "selection_non_overlapping_4h.parquet", index=False)
        except Exception:
            _write_csv(out_dir / "selection_non_overlapping_4h.csv", non_ov)

        # load candle span covering all triggers + 4h
        trig_ns = [int(e["trigger_ts_ns"]) for e in events if e.get("trigger_ts_ns") is not None]
        start_ns = min(trig_ns) - 60 * NS
        end_ns = max(trig_ns) + (240 + 5) * 60 * NS
        log(f"loading candles {ns_to_dt(start_ns)} -> {ns_to_dt(end_ns)}")
        candles = load_candles_1m(
            client,
            start=ns_to_dt(start_ns),
            end=ns_to_dt(end_ns),
            symbol=params.symbol,
            exchange=params.exchange,
        )
        log(f"loaded candles n={len(candles)}")

        pilot_events = select_pilot(events, wall_flags, max_n=params.pilot_max_events)
        assert len(pilot_events) <= PILOT_MAX_EVENTS
        pilot_t0 = time.time()
        pilot_rows = []
        for e in pilot_events:
            res = analyze_event_path(
                candles,
                event_id=e["event_id"],
                trigger_ts_ns=int(e["trigger_ts_ns"]),
                trigger_price=float(e["trigger_price"]),
                trade_side=str(e["trade_side"]),
            )
            h240 = next((h for h in res["horizons"] if h["horizon_min"] == 240), {})
            pilot_rows.append(
                {
                    "event_id": e["event_id"],
                    "label_price_only": e.get("label_price_only"),
                    "trade_side": e.get("trade_side"),
                    "wall_persistence": wall_flags.get(e["event_id"]),
                    "ok": res["ok"],
                    "entry_candle_partial": res.get("entry_candle_partial"),
                    "mfe_240_pct": h240.get("mfe_pct"),
                    "mae_240_pct": h240.get("mae_pct"),
                    "mae_before_mfe_ex_pct": h240.get("mae_before_mfe_peak_exclusive_pct"),
                    "close_240_pct": h240.get("close_return_pct"),
                }
            )
            log(f"pilot {e['event_id']} ok={res['ok']} mfe240={h240.get('mfe_pct')}")
        pilot_runtime = time.time() - pilot_t0
        _write_csv(out_dir / "pilot_events.csv", pilot_rows)
        pilot_ok = all(r["ok"] for r in pilot_rows) and len(pilot_rows) > 0
        (out_dir / "pilot_report.md").write_text(
            "\n".join(
                [
                    "# Price Path 4h Pilot",
                    "",
                    f"- events: {len(pilot_rows)} (max {params.pilot_max_events})",
                    f"- all_ok: {pilot_ok}",
                    f"- runtime_s: {pilot_runtime:.3f}",
                    f"- peak_rss_mib: {_peak_rss_mib():.1f}",
                    f"- candle_tf: {params.candle_timeframe}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        if not pilot_ok:
            verdict = "PRICE_PATH_4H_BLOCKED_IMPLEMENTATION"
            write_report(out_dir, verdict=verdict, summary={"pilot_ok": False})
            return {"verdict": verdict, "out_dir": str(out_dir)}
        if pilot_only:
            return {"verdict": "PRICE_PATH_4H_SUCCESS_LOW_SAMPLE", "out_dir": str(out_dir)}

        # Full
        path_rows: list[dict[str, Any]] = []
        horizon_rows: list[dict[str, Any]] = []
        target_rows: list[dict[str, Any]] = []
        hit_rows: list[dict[str, Any]] = []
        uw_rows: list[dict[str, Any]] = []
        missing_price = []
        full_t0 = time.time()
        for i, e in enumerate(events):
            if e.get("trigger_price") is None or e.get("trigger_ts_ns") is None:
                missing_price.append(e["event_id"])
                continue
            if e.get("trade_side") not in ("LONG", "SHORT"):
                continue
            res = analyze_event_path(
                candles,
                event_id=e["event_id"],
                trigger_ts_ns=int(e["trigger_ts_ns"]),
                trigger_price=float(e["trigger_price"]),
                trade_side=str(e["trade_side"]),
            )
            path_rows.extend(res["path_rows"])
            horizon_rows.extend(res["horizons"])
            target_rows.extend(res["targets"])
            hit_rows.extend(res["first_hits"])
            if res["underwater"]:
                uw_rows.append(res["underwater"])
            if (i + 1) % 40 == 0:
                log(f"progress {i+1}/{len(events)} rss={_peak_rss_mib():.1f}")
        full_runtime = time.time() - full_t0

        import pandas as pd

        pd.DataFrame(path_rows).to_parquet(out_dir / "event_price_paths.parquet", index=False)
        pd.DataFrame(horizon_rows).to_parquet(out_dir / "event_horizon_excursions.parquet", index=False)
        pd.DataFrame(target_rows).to_parquet(out_dir / "event_target_paths.parquet", index=False)
        pd.DataFrame(hit_rows).to_parquet(out_dir / "first_hit_matrix.parquet", index=False)
        pd.DataFrame(uw_rows).to_parquet(out_dir / "event_underwater_metrics.parquet", index=False)

        # groups
        by_id = {e["event_id"]: e for e in events}
        non_ov_map = {r["event_id"]: r for r in non_ov}

        def ids_for(group: str) -> set[str]:
            if group == "WALL_PERSISTENCE_TRUE":
                return {eid for eid, ok in wall_flags.items() if ok}
            if group == "WALL_PERSISTENCE_FALSE":
                return {eid for eid, ok in wall_flags.items() if not ok}
            if group.startswith("LABEL_"):
                lab = group[len("LABEL_") :]
                return {e["event_id"] for e in events if e.get("label_price_only") == lab}
            if group in ("LONG", "SHORT"):
                return {e["event_id"] for e in events if e.get("trade_side") == group}
            if group in ("UPPER", "LOWER"):
                return {e["event_id"] for e in events if e.get("event_role") == group}
            if group.startswith("CONF_"):
                conf = group[len("CONF_") :]
                return {e["event_id"] for e in events if e.get("confluence_class") == conf}
            if group == "FIRST_TOUCH":
                return policy_ids(
                    events=events, episodes=episodes, non_ov_4h=non_ov, name="FIRST_TOUCH_PER_ZONE_PROFILE_VERSION"
                )
            if group == "NON_OVERLAPPING_4H":
                return policy_ids(
                    events=events, episodes=episodes, non_ov_4h=non_ov, name="NON_OVERLAPPING_4H"
                )
            if group == "ALL_VALID":
                return policy_ids(
                    events=events, episodes=episodes, non_ov_4h=non_ov, name="ALL_VALID_EVENTS"
                )
            raise KeyError(group)

        primary = policy_ids(
            events=events,
            episodes=episodes,
            non_ov_4h=non_ov,
            name="FIRST_TOUCH_PER_ZONE_PROFILE_VERSION",
        )
        groups = [
            "ALL_VALID",
            "FIRST_TOUCH",
            "NON_OVERLAPPING_4H",
            "WALL_PERSISTENCE_TRUE",
            "WALL_PERSISTENCE_FALSE",
            "LABEL_ABSORB",
            "LABEL_FAILED_BREAK",
            "LABEL_TRUE_BREAK",
            "LONG",
            "SHORT",
            "UPPER",
            "LOWER",
            "CONF_C1_30M",
            "CONF_C1_1H",
            "CONF_C1_4H",
            "CONF_C2_30M_1H",
            "CONF_C2_30M_4H",
            "CONF_C2_1H_4H",
            "CONF_C3_30M_1H_4H",
        ]
        # intersect label/side groups with first-touch for primary reporting clarity? Spec says report separately - use all valid for labels/sides, primary policies as named.

        summary_by_horizon = []
        summary_by_label = []
        summary_by_side = []
        summary_by_conf = []
        summary_by_wall = []
        target_reach = []
        mae_before = []
        first_hit_sum = []

        for g in groups:
            try:
                gids = ids_for(g)
            except KeyError:
                continue
            # for label/side/conf/wall use intersection with events that have path
            for h in HORIZONS_MIN:
                row = summarize_horizons(horizon_rows, group_name=g, event_ids=gids, horizon_min=h)
                summary_by_horizon.append(row)
                if g.startswith("LABEL_"):
                    summary_by_label.append(row)
                if g in ("LONG", "SHORT"):
                    summary_by_side.append(row)
                if g.startswith("CONF_"):
                    summary_by_conf.append(row)
                if g.startswith("WALL_"):
                    summary_by_wall.append(row)
            for t in MFE_TARGETS_PCT:
                tr = summarize_targets(target_rows, group_name=g, event_ids=gids, target_pct=t)
                target_reach.append(tr)
                mae_before.append(
                    {
                        "group": g,
                        "target_pct": t,
                        "median_mae_before_target_pct": tr["median_mae_before_target_pct"],
                        "q75_mae_before_target_pct": tr["q75_mae_before_target_pct"],
                        "q90_mae_before_target_pct": tr["q90_mae_before_target_pct"],
                        "reach_rate": tr["reach_rate"],
                        "reached_count": tr["reached_count"],
                        "event_count": tr["event_count"],
                        "sample_flag": tr["sample_flag"],
                    }
                )
            for h in FIRST_HIT_HORIZONS_MIN:
                for pair in ((0.10, 0.10), (0.20, 0.15), (0.25, 0.25), (0.50, 0.50)):
                    first_hit_sum.append(
                        summarize_first_hit(
                            hit_rows,
                            group_name=g,
                            event_ids=gids,
                            horizon_min=h,
                            target_pct=pair[0],
                            stop_pct=pair[1],
                        )
                    )

        _write_csv(out_dir / "summary_by_horizon.csv", summary_by_horizon)
        _write_csv(out_dir / "summary_by_label.csv", summary_by_label)
        _write_csv(out_dir / "summary_by_trade_side.csv", summary_by_side)
        _write_csv(out_dir / "summary_by_confluence.csv", summary_by_conf)
        _write_csv(out_dir / "summary_by_wall_persistence.csv", summary_by_wall)
        _write_csv(out_dir / "target_reach_summary.csv", target_reach)
        _write_csv(out_dir / "mae_before_mfe_summary.csv", mae_before)
        _write_csv(out_dir / "first_hit_summary.csv", first_hit_sum)

        # cost adjusted close returns at horizons for FIRST_TOUCH
        cost_rows = []
        for h in HORIZONS_MIN:
            for eid in primary:
                hr = next(
                    (
                        r
                        for r in horizon_rows
                        if r["event_id"] == eid and int(r["horizon_min"]) == h and r.get("is_complete")
                    ),
                    None,
                )
                if not hr:
                    continue
                gross = hr.get("close_return_pct")
                if gross is None:
                    continue
                for c in COST_PCT:
                    cost_rows.append(
                        {
                            "event_id": eid,
                            "horizon_min": h,
                            "gross_close_return_pct": gross,
                            "roundtrip_cost_pct": c,
                            "net_return_pct": float(gross) - float(c),
                        }
                    )
        # required gross MFE for net targets
        for c in COST_PCT:
            for net in NET_TARGETS_PCT:
                cost_rows.append(
                    {
                        "event_id": None,
                        "horizon_min": None,
                        "gross_close_return_pct": None,
                        "roundtrip_cost_pct": c,
                        "net_return_pct": None,
                        "required_gross_mfe_pct_for_net": float(net) + float(c),
                        "target_net_pct": net,
                    }
                )
        _write_csv(out_dir / "cost_adjusted_close_returns.csv", cost_rows)

        # summary numbers for verdict
        def med_h(group: str, h: int, field: str) -> float | None:
            rows = [
                r
                for r in summary_by_horizon
                if r["group"] == group and int(r["horizon_min"]) == h
            ]
            return None if not rows else rows[0].get(field)

        complete_4h = sum(
            1
            for r in horizon_rows
            if int(r["horizon_min"]) == 240 and r.get("is_complete")
        )
        wall_n = sum(1 for v in wall_flags.values() if v)
        non4 = sum(1 for r in non_ov if r.get("selected_non_overlapping_4h"))
        amb_rate = None
        amb_rows = [r for r in horizon_rows if int(r["horizon_min"]) == 240 and r.get("is_complete")]
        if amb_rows:
            amb_rate = sum(1 for r in amb_rows if r.get("intrabar_order_ambiguous")) / len(amb_rows)

        # low sample if primary complete < 50
        ft_complete = med_h("FIRST_TOUCH", 240, "complete_count") or 0
        verdict = "PRICE_PATH_4H_SUCCESS"
        if ft_complete < 50:
            verdict = "PRICE_PATH_4H_SUCCESS_LOW_SAMPLE"

        total_runtime = time.time() - t0
        runtime = {
            "pilot_runtime_s": pilot_runtime,
            "full_runtime_s": full_runtime,
            "total_runtime_s": total_runtime,
            "peak_rss_mib": _peak_rss_mib(),
            "n_candles_loaded": len(candles),
        }
        _write_json(out_dir / "runtime_metrics.json", runtime)

        summary = {
            "candle_source": candle_audit["full_name"],
            "candle_timeframe": candle_audit["selected_timeframe"],
            "candle_range": candle_audit["global_range"],
            "input_events": len(events),
            "events_complete_4h": complete_4h,
            "first_touch_events": len(primary),
            "non_overlapping_4h_events": non4,
            "wall_persistence_events": wall_n,
            "missing_trigger_price": missing_price,
            "median_mfe_pct": {
                "30m": med_h("FIRST_TOUCH", 30, "median_mfe_pct"),
                "60m": med_h("FIRST_TOUCH", 60, "median_mfe_pct"),
                "120m": med_h("FIRST_TOUCH", 120, "median_mfe_pct"),
                "240m": med_h("FIRST_TOUCH", 240, "median_mfe_pct"),
            },
            "median_mae_pct": {
                "30m": med_h("FIRST_TOUCH", 30, "median_mae_pct"),
                "60m": med_h("FIRST_TOUCH", 60, "median_mae_pct"),
                "120m": med_h("FIRST_TOUCH", 120, "median_mae_pct"),
                "240m": med_h("FIRST_TOUCH", 240, "median_mae_pct"),
            },
            "median_mae_before_mfe_peak_pct_240": med_h(
                "FIRST_TOUCH", 240, "median_mae_before_mfe_peak_pct"
            ),
            "intrabar_ambiguous_rate_240": amb_rate,
            "frozen_wall_filter": frozen,
            "units": "percent",
        }
        # target reach for FT
        for t in (0.10, 0.25, 0.50):
            tr = next(
                (
                    r
                    for r in target_reach
                    if r["group"] == "FIRST_TOUCH" and abs(float(r["target_pct"]) - t) < 1e-12
                ),
                None,
            )
            summary[f"target_{t:.2f}"] = tr

        _write_json(
            out_dir / "run_manifest.json",
            {
                "run_id": RUN_ID,
                "package": PACKAGE_NAME,
                "verdict": verdict,
                "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "params": params.to_dict(),
                "input_audit": input_audit,
                "candle_audit": candle_audit,
                "runtime": runtime,
                "summary": summary,
                "notes": [
                    "MP batch and OB enrichment read-only",
                    "WALL_PERSISTENCE thresholds frozen unchanged",
                    "All user metrics in percent (not bps)",
                    "Outcomes may extend beyond Silver windows via candle history",
                ],
            },
        )
        write_report(out_dir, verdict=verdict, summary=summary, runtime=runtime)
        log(f"done verdict={verdict}")
        return {"verdict": verdict, "out_dir": str(out_dir), "summary": summary}
    except Exception as exc:  # noqa: BLE001
        log("ERROR " + traceback.format_exc())
        if "MEMORY" in str(exc).upper() or "RESOURCE" in str(exc).upper():
            verdict = "PRICE_PATH_4H_RESOURCE_ABORT"
        else:
            verdict = "PRICE_PATH_4H_BLOCKED_IMPLEMENTATION"
        _write_json(out_dir / "run_manifest.json", {"verdict": verdict, "error": str(exc)})
        write_report(out_dir, verdict=verdict, summary={"error": str(exc)})
        return {"verdict": verdict, "out_dir": str(out_dir), "error": str(exc)}
    finally:
        if own_client and client is not None:
            try:
                client.close()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="mp_price_path_4h_v1")
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--pilot-only", action="store_true")
    p.add_argument("--pilot-max", type=int, default=PILOT_MAX_EVENTS)
    args = p.parse_args(argv)
    repo = args.repo_root.resolve()
    params = PathParams(
        batch_run_dir=repo / BATCH_RUN_REL,
        enrich_run_dir=repo / ENRICH_RUN_REL,
        out_dir=repo / DEFAULT_RUN_REL,
        pilot_max_events=min(args.pilot_max, PILOT_MAX_EVENTS),
    )
    result = run_price_path(repo_root=repo, params=params, pilot_only=args.pilot_only)
    print(json.dumps({"verdict": result.get("verdict"), "out_dir": result.get("out_dir")}, indent=2))
    return 0 if str(result.get("verdict", "")).startswith("PRICE_PATH_4H_SUCCESS") else 2
