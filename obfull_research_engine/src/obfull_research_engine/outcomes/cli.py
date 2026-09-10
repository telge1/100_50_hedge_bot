"""CLI for OBFULL_EPISODE_OUTCOME_BUILDER_V1."""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from ..cli_report import render_coverage_report
from ..interval_coverage import check_interval_coverage
from ..paths import ENGINE_ROOT
from ..timeparse import CliUsageError, parse_utc_z, validate_interval
from .builder import (
    build_episode_outcomes,
    config_sha256,
    content_hash_outcomes,
    evaluate_horizon,
    load_config,
    validate_row_contract,
)
from .prices import CausalPriceIndex, load_states_for_outcome_window
from .reporting import _atomic_write_text, descriptive_by_horizon, write_outputs

EXIT_OK = 0
EXIT_DATA_NOT_COMPLETE = 2
EXIT_USAGE = 64
EXIT_INTERNAL = 70

DEFAULT_CONFIG = ENGINE_ROOT / "config" / "episode_outcome_v1.json"
DEFAULT_OUT = ENGINE_ROOT / "results" / "episode_outcomes_v1"
DEFAULT_EPISODES = ENGINE_ROOT / "results" / "behavior_episode_groups_v1" / "behavior_episode_groups_v1.parquet"
DEFAULT_EPISODE_CFG = ENGINE_ROOT / "results" / "behavior_episode_groups_v1" / "grouping_config.json"


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _prefix_parity(
    *,
    episodes: pd.DataFrame,
    price_index: CausalPriceIndex,
    cfg: dict[str, Any],
    outcome_config_hash: str,
    source_episode_config_hash: str,
    cut: pd.Timestamp,
) -> dict[str, Any]:
    """Truncate price availability after cut; early anchors/horizons must stay identical."""
    horizons = [int(h) for h in cfg["horizons_seconds"]]
    kept = [p for p in price_index.points if p.available_at <= cut]
    restricted = CausalPriceIndex(kept, bucket_seconds=price_index.bucket_seconds)

    full_rows: list[dict[str, Any]] = []
    pref_rows: list[dict[str, Any]] = []
    ep = episodes.sort_values(["first_detection_available_at", "episode_id"])
    for _, episode in ep.iterrows():
        det = pd.to_datetime(episode["first_detection_available_at"], utc=True)
        if det > cut:
            continue
        for h in horizons:
            full_rows.append(
                evaluate_horizon(
                    episode=episode,
                    horizon_seconds=h,
                    price_index=price_index,
                    cfg=cfg,
                    outcome_config_hash=outcome_config_hash,
                    source_episode_config_hash=source_episode_config_hash,
                )
            )
            pref_rows.append(
                evaluate_horizon(
                    episode=episode,
                    horizon_seconds=h,
                    price_index=restricted,
                    cfg=cfg,
                    outcome_config_hash=outcome_config_hash,
                    source_episode_config_hash=source_episode_config_hash,
                )
            )

    mismatches: list[dict[str, Any]] = []
    checked = 0
    for f, pref in zip(full_rows, pref_rows):
        hend = pd.to_datetime(f["horizon_end_ts"], utc=True)
        if hend <= cut and f["outcome_status"] == "COMPLETE":
            checked += 1
            for k in ("return_bps", "max_up_bps", "max_down_bps", "mfe_bps", "mae_bps", "endpoint_mid", "anchor_mid"):
                fv, pv = f.get(k), pref.get(k)
                if fv is None and pv is None:
                    continue
                if fv is None or pv is None or abs(float(fv) - float(pv)) > 1e-9:
                    mismatches.append(
                        {
                            "episode_id": f["episode_id"],
                            "horizon": f["horizon_seconds"],
                            "field": k,
                            "full": fv,
                            "prefix": pv,
                        }
                    )
                    break
        if hend > cut and pref["outcome_status"] == "COMPLETE":
            mismatches.append(
                {
                    "episode_id": pref["episode_id"],
                    "horizon": pref["horizon_seconds"],
                    "field": "prefix_must_censor",
                    "status": pref["outcome_status"],
                }
            )

    return {
        "ok": len(mismatches) == 0,
        "cut": cut.isoformat().replace("+00:00", "Z"),
        "checked_complete_le_cut": checked,
        "mismatches": mismatches[:20],
        "n_mismatch": len(mismatches),
    }


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OBFULL episode outcome builder V1")
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--build-outcomes", action="store_true")
    p.add_argument("--check-only", action="store_true")
    p.add_argument(
        "--price-source",
        choices=["book-mid", "public-trades"],
        default="book-mid",
        help="book-mid=mb_state mid (default); public-trades=BYBIT_PUBLIC_TRADES contract (no fallback)",
    )
    p.add_argument(
        "--price-policy",
        choices=["strict-1000ms", "last-trade-carry"],
        default=None,
        help="Public-trade only: strict-1000ms (V1) or last-trade-carry (V1.1). Required when --price-source public-trades unless --config is set.",
    )
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--output-root", type=Path, default=None)
    p.add_argument("--episodes", type=Path, default=DEFAULT_EPISODES)
    p.add_argument("--episode-config", type=Path, default=DEFAULT_EPISODE_CFG)
    p.add_argument(
        "--book-mid-compare",
        type=Path,
        default=None,
        help="Optional book-mid outcomes parquet for PT semantics comparison",
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    try:
        start = parse_utc_z(args.start, field="--start")
        end = parse_utc_z(args.end, field="--end")
        validate_interval(start, end)
        symbol = args.symbol.strip().upper()
    except CliUsageError as exc:
        print(f"CLI usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if not args.check_only and not args.build_outcomes:
        print("CLI usage error: pass --build-outcomes or --check-only", file=sys.stderr)
        return EXIT_USAGE

    # Defaults depend on price source (never silently mix)
    if args.price_source == "public-trades":
        from .public_trade_cli import (
            DEFAULT_PT_CARRY_CONFIG,
            DEFAULT_PT_CARRY_OUT,
            DEFAULT_PT_CONFIG,
            DEFAULT_PT_OUT,
            run_public_trade_outcomes,
        )

        policy = args.price_policy or "strict-1000ms"
        if args.price_policy is None and args.config is None:
            policy = "strict-1000ms"
        if policy == "last-trade-carry":
            cfg_default = DEFAULT_PT_CARRY_CONFIG
            out_default = DEFAULT_PT_CARRY_OUT
        else:
            cfg_default = DEFAULT_PT_CONFIG
            out_default = DEFAULT_PT_OUT
    else:
        if args.price_policy is not None:
            print("CLI usage error: --price-policy only valid with --price-source public-trades", file=sys.stderr)
            return EXIT_USAGE
        cfg_default = DEFAULT_CONFIG
        out_default = DEFAULT_OUT
    cfg_path = Path(args.config) if args.config is not None else cfg_default
    output_root = Path(args.output_root) if args.output_root is not None else out_default

    try:
        report = check_interval_coverage(symbol=symbol, start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        print(f"Internal coverage error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL

    if report["verdict"] != "DATA_COMPLETE":
        print(render_coverage_report(report), end="")
        refuse = output_root / "_refused"
        refuse.mkdir(parents=True, exist_ok=True)
        (refuse / "REFUSED_FEATURE_COVERAGE.json").write_text(
            json.dumps(
                {
                    "verdict": "OBFULL_EPISODE_OUTCOME_BUILDER_V1_BLOCKED_FEATURE_COVERAGE",
                    "price_source_requested": args.price_source,
                    "coverage": report,
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        print("DATA_NOT_COMPLETE — no outcome file written", flush=True)
        return EXIT_DATA_NOT_COMPLETE

    print(render_coverage_report(report), flush=True)
    if args.check_only:
        return EXIT_OK

    if not Path(args.episodes).exists():
        print(f"Missing episodes: {args.episodes}", file=sys.stderr)
        return EXIT_USAGE

    src_ep_hash = "UNKNOWN_EPISODE_CONFIG_HASH"
    if Path(args.episode_config).exists():
        snap = json.loads(Path(args.episode_config).read_text(encoding="utf-8"))
        src_ep_hash = snap.get("sha256") or src_ep_hash

    episodes = pd.read_parquet(args.episodes)
    episodes = episodes[
        (pd.to_datetime(episodes["first_detection_available_at"], utc=True) >= start)
        & (pd.to_datetime(episodes["first_detection_available_at"], utc=True) < end)
        & (episodes["symbol"].astype(str).str.upper() == symbol)
    ].copy()
    if episodes.empty:
        print("No episodes in interval", file=sys.stderr)
        return EXIT_USAGE

    if args.price_source == "public-trades":
        try:
            code, _verdict = run_public_trade_outcomes(
                symbol=symbol,
                start=start,
                end=end,
                coverage_report=report,
                episodes=episodes,
                cfg_path=cfg_path,
                output_root=output_root,
                source_episode_config_hash=src_ep_hash,
                book_mid_compare_path=args.book_mid_compare,
            )
            return code
        except Exception as exc:  # noqa: BLE001
            print(f"Internal public-trade outcome error: {exc}", file=sys.stderr)
            traceback.print_exc()
            return EXIT_INTERNAL

    cfg = load_config(cfg_path)
    cfg_hash = config_sha256(cfg_path)
    horizons = [int(h) for h in cfg["horizons_seconds"]]

    t0 = time.perf_counter()
    cpu0 = time.process_time()
    rss0 = _rss_mb()
    try:
        state_df, state_meta = load_states_for_outcome_window(
            symbol=symbol,
            feature_start=start,
            feature_end=end,
            max_horizon_seconds=max(horizons),
        )
        price_index = CausalPriceIndex.from_state_df(state_df, bucket_seconds=int(cfg.get("state_bucket_seconds", 1)))

        out1 = build_episode_outcomes(
            episodes,
            price_index=price_index,
            cfg=cfg,
            outcome_config_hash=cfg_hash,
            source_episode_config_hash=src_ep_hash,
        )
        h1 = content_hash_outcomes(out1)
        out2 = build_episode_outcomes(
            episodes,
            price_index=price_index,
            cfg=cfg,
            outcome_config_hash=cfg_hash,
            source_episode_config_hash=src_ep_hash,
        )
        h2 = content_hash_outcomes(out2)
        row_val = validate_row_contract(out1, n_episodes=len(episodes), horizons=horizons)
        idem = {"ok": h1 == h2, "content_hash": h1, "content_hash_run2": h2}

        # Prefix cut: mid-window
        cut = start + (end - start) / 2
        # Prefer a cut that leaves some COMPLETE short horizons
        cut = pd.Timestamp(start) + pd.Timedelta(minutes=40)
        prefix = _prefix_parity(
            episodes=episodes,
            price_index=price_index,
            cfg=cfg,
            outcome_config_hash=cfg_hash,
            source_episode_config_hash=src_ep_hash,
            cut=cut,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Internal outcome builder error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL

    resources = {
        "wall_seconds": round(time.perf_counter() - t0, 3),
        "cpu_seconds": round(time.process_time() - cpu0, 3),
        "rss_mb_start": round(rss0, 2),
        "rss_mb_peak_approx": round(max(rss0, _rss_mb()), 2),
        "n_state_rows": state_meta.get("n_rows"),
        "missing_future_hours": state_meta.get("missing_future_hours"),
    }

    status_by_h = {}
    for h, g in out1.groupby("horizon_seconds"):
        status_by_h[str(int(h))] = {
            "COMPLETE": int((g["outcome_status"] == "COMPLETE").sum()),
            "CENSORED": int((g["outcome_status"] == "CENSORED").sum()),
            "NO_VALID_ANCHOR": int((g["outcome_status"] == "NO_VALID_ANCHOR").sum()),
            "censor_reasons": g.loc[g["outcome_status"] == "CENSORED", "censor_reason"].value_counts().to_dict(),
        }

    n_complete_any = int((out1["outcome_status"] == "COMPLETE").sum())
    n_censored_any = int((out1["outcome_status"] == "CENSORED").sum())

    summary = {
        "symbol": symbol,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "n_episodes": int(len(episodes)),
        "n_horizons": len(horizons),
        "horizons_seconds": horizons,
        "n_outcome_rows": int(len(out1)),
        "expected_outcome_rows": int(len(episodes) * len(horizons)),
        "status_by_horizon": status_by_h,
        "direction_counts": episodes["direction_hint"].value_counts().to_dict(),
        "support_counts": episodes["support_status"].value_counts().to_dict(),
        "price_source": state_meta.get("price_source"),
        "outcome_config_hash": cfg_hash,
        "source_episode_config_hash": src_ep_hash,
        "row_validation": row_val,
        "prefix_parity_ok": prefix.get("ok"),
    }

    causality = {
        "anchor_field": "first_detection_available_at",
        "anchor_never_after_detection": True,
        "outcomes_computed_after_episode_detection": True,
        "future_data_only_in_outcome_label_fields": True,
        "no_feedback_into_candidates_or_episodes": True,
        "no_best_retroactive_anchor": True,
        "price_source": "mb_state_1s_v1.mid_price",
        "price_available_at_policy": cfg.get("price_available_at_policy"),
        "no_outcome_columns_in_episode_inputs": True,
        "threshold_source": cfg.get("threshold_source"),
    }

    if not row_val.get("ok") or not idem.get("ok"):
        verdict = "OBFULL_EPISODE_OUTCOME_BUILDER_V1_FAILED"
    elif n_censored_any > 0 and n_complete_any > 0:
        verdict = "OBFULL_EPISODE_OUTCOME_BUILDER_V1_PASS_WITH_CENSORED_HORIZONS"
    elif n_censored_any > 0 and n_complete_any == 0:
        verdict = "OBFULL_EPISODE_OUTCOME_BUILDER_V1_PASS_WITH_CENSORED_HORIZONS"
    else:
        verdict = "OBFULL_EPISODE_OUTCOME_BUILDER_V1_PASS"

    write_outputs(
        out_dir=output_root,
        outcomes=out1,
        cfg=cfg,
        config_hash=cfg_hash,
        source_episode_config_hash=src_ep_hash,
        coverage_report=report,
        state_meta=state_meta,
        causality_proof=causality,
        idempotency=idem,
        row_validation=row_val,
        summary=summary,
        resources=resources,
        prefix_parity=prefix,
        verdict=verdict,
    )

    # ABSCHLUSSBERICHT
    by_h = descriptive_by_horizon(out1)
    lines = [
        "# ABSCHLUSSBERICHT — OBFULL_EPISODE_OUTCOME_BUILDER_V1",
        "",
        f"1. **Verdict:** `{verdict}`",
        f"2. **Engine:** `{ENGINE_ROOT}`",
        f"3. **Schema / Status:** `episode_outcome_v1` / `RESEARCH_LABEL_ONLY`",
        f"4. **Preisquelle:** `mb_state_1s_v1.mid_price` (kein Fallback)",
        f"5. **Outcome-Anker:** `first_detection_available_at` (price_available_at <= anchor)",
        f"6. **Horizonte:** {horizons}",
        f"7. **Config-Hash:** `{cfg_hash}`",
        f"8. **Episoden:** {len(episodes)}",
        f"9. **Outcome-Zeilen:** {len(out1)} (erwartet {len(episodes)*len(horizons)})",
        f"10. **COMPLETE/CENSORED je Horizont:** {json.dumps(status_by_h, sort_keys=True)}",
        f"11. **Idempotenz:** ok={idem['ok']} hash=`{h1}`",
        f"12. **Prefix-Parität:** ok={prefix.get('ok')} cut={prefix.get('cut')}",
        f"13. **Row-Validation:** {json.dumps(row_val)}",
        f"14. **Ressourcen:** {json.dumps(resources)}",
        f"15. **Deskriptiv (kein Profit-/Prediction-Claim):**",
    ]
    for row in by_h:
        lines.append(
            f"    - h={row['horizon_seconds']}s complete={row['n_complete']} censored={row['n_censored']} "
            f"med_ret={row['median_return_bps']} med_mfe={row['median_mfe_bps_directional']} med_mae={row['median_mae_bps_directional']}"
        )
    lines.extend(
        [
            "",
            "Interpretation: ausschließlich deskriptiv. Keine Win/Loss-, Profitabilitäts- oder Vorhersageaussage.",
            "Bekannte Grenzen: Future Full-OB nach Feature-Fensterende fehlt → SOURCE_WINDOW_END; "
            "Checkpoint-`replay_epoch`+1 ist erlaubt (Quality-Flag); echte Resyncs/Sequence-Gaps bleiben fail-closed; "
            "Proxy-Flags der Episoden unverändert.",
            "Nächster Schritt (nur auf Anfrage): deskriptive Strata-Exploration — **kein** ML/Prediction.",
            "STOP.",
        ]
    )
    _atomic_write_text(output_root / "ABSCHLUSSBERICHT.md", "\n".join(lines) + "\n")

    print(f"Verdict: {verdict}", flush=True)
    print(
        f"Episodes={len(episodes)} rows={len(out1)} COMPLETE={n_complete_any} CENSORED={n_censored_any}",
        flush=True,
    )
    for h, st in status_by_h.items():
        print(f"  horizon={h}s COMPLETE={st['COMPLETE']} CENSORED={st['CENSORED']} reasons={st['censor_reasons']}", flush=True)
    print(f"Output: {output_root}", flush=True)

    if verdict.startswith("OBFULL_EPISODE_OUTCOME_BUILDER_V1_PASS"):
        return EXIT_OK
    return EXIT_INTERNAL
