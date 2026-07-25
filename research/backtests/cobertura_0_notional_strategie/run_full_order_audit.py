"""Full order / position / economics double-check audit for APT net-BE runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv

from .config import CoberturaConfig, default_apt_example
from .manual_order_timeline import generate_manual_timelines
from .order_audit import AVG_TOL, FEE_TOL, PNL_TOL, QTY_TOL, reconstruct_audit
from .run_net_be_policy_comparison import SAFETY_BUFFER_USDT, _policy_specs, _variant_cfg
from .runner import run_cobertura

OUT_NAME = "apt_full_order_audit_20260725"
# Representative net-BE target used for the audit (matches comparison grid).
AUDIT_TARGET_USDT = 0.0


def _write_walkthrough(path: Path, bundle: Any) -> None:
    cfg = bundle.cfg
    lines = [
        f"# {bundle.policy} — Full Order Walkthrough",
        "",
        f"- run_id: `{cfg.run_id}`",
        f"- overlay_exit_policy: `{cfg.overlay_exit_policy}`",
        f"- full_exit_target_mode: `{cfg.full_exit_target_mode}`",
        f"- target: {cfg.full_exit_target_usdt} USDT",
        f"- safety_buffer: {cfg.full_exit_safety_buffer_usdt} USDT",
        f"- final_status: `{bundle.result.state}` / `{bundle.result.exit_reason}`",
        f"- bars: {bundle.result.bars_processed}",
        f"- fills: {len(bundle.result.fill_events)}",
        "",
        "## Event order (within candle)",
        "",
        "1. Activate pending TP / shared-BE triggers from prior bar",
        "2. Arm recovery round if activation level touched",
        "3. Process overlay exits (shared BE or individual TP)",
        "4. Net-BE full-exit gate (before adds)",
        "5. Short adds shallow → deep at fixed level triggers",
        "6. Legacy post-add full-exit skipped under `net_be`",
        "",
        "## Chronological fills",
        "",
    ]
    lines.extend(bundle.walkthrough_lines)
    lines += [
        "",
        "## Full-exit summary",
        "",
    ]
    for row in bundle.full_exit_audit:
        for k, v in row.items():
            lines.append(f"- **{k}**: `{v}`")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_report(path: Path, summaries: list[dict[str, Any]], bundles: list[Any]) -> None:
    lines = [
        "# APT Full Order Audit (Netto-BE)",
        "",
        "Read-only double-check of executed orders, averages, fees, PnL, and "
        "final net-BE exit for `shared_be`, `individual_tp_2p00`, and "
        "`individual_tp_scaled` on the canonical APT seed.",
        "",
        "## Tolerances",
        "",
        f"- AVG_TOL = {AVG_TOL}",
        f"- FEE_TOL = {FEE_TOL}",
        f"- PNL_TOL = {PNL_TOL}",
        f"- QTY_TOL = {QTY_TOL}",
        "",
        "## Per-policy verdict",
        "",
        "| policy | status | flat | invariant_fails | fee_fails | avg_fails | overall |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s['policy']} | {s['final_status']} | {s['flat_after_exit']} | "
            f"{s['invariant_fail_count']} | {s['fee_audit_fail_count']} | "
            f"{s['avg_audit_fail_count']} | "
            f"{'PASS' if s['audit_overall_pass'] else 'FAIL'} |"
        )
    lines += [
        "",
        "## Answers",
        "",
    ]
    for s in summaries:
        lines.append(
            f"- **{s['policy']}**: overall "
            f"{'PASS' if s['audit_overall_pass'] else 'FAIL'}; "
            f"fee_ledger_match={s['fee_ledger_match']}; "
            f"first_BE={None if not s.get('first_net_be_touch') else s['first_net_be_touch'].get('timestamp')}; "
            f"ambiguous_intrabar={s['ambiguous_intrabar_count']}."
        )
    lines += [
        "",
        "## Event order (all policies under net_be)",
        "",
        summaries[0]["event_order_documented"] if summaries else "",
        "",
        "## Artifacts",
        "",
        "See CSV/JSON siblings in this folder plus per-policy walkthroughs.",
        "",
    ]
    # Highlight any violations
    any_fail = False
    for b in bundles:
        if b.invariant_violations:
            any_fail = True
            lines.append(f"### Violations — {b.policy}")
            lines.append("")
            for v in b.invariant_violations[:50]:
                lines.append(f"- {v}")
            lines.append("")
    if not any_fail:
        lines.append("No invariant violations recorded.")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def run_full_order_audit(
    *,
    output_dir: str | Path | None = None,
    target: float = AUDIT_TARGET_USDT,
    safety_buffer: float = SAFETY_BUFFER_USDT,
) -> dict[str, Any]:
    base = default_apt_example()
    out = Path(
        output_dir
        or Path(__file__).resolve().parent / "results" / OUT_NAME
    )
    out.mkdir(parents=True, exist_ok=True)

    candles = load_candles_for_symbol(
        base.symbol,
        timeframe=base.timeframe,
        data_dir=DEFAULT_DATA_DIR,
        limit=base.candle_limit,
    )

    bundles = []
    summaries: list[dict[str, Any]] = []
    all_lifecycle: list[dict[str, Any]] = []
    all_fills: list[dict[str, Any]] = []
    all_pos: list[dict[str, Any]] = []
    all_avg: list[dict[str, Any]] = []
    all_pnl: list[dict[str, Any]] = []
    all_fee: list[dict[str, Any]] = []
    all_trig: list[dict[str, Any]] = []
    all_tranche: list[dict[str, Any]] = []
    all_exit: list[dict[str, Any]] = []
    all_viol: list[dict[str, Any]] = []
    all_amb: list[dict[str, Any]] = []
    all_shared: list[dict[str, Any]] = []
    integrity: dict[str, Any] = {"policies": {}}

    walk_names = {
        "shared_be": "shared_be_walkthrough.md",
        "individual_tp_2p00": "individual_tp_2p00_walkthrough.md",
        "individual_tp_scaled": "individual_tp_scaled_walkthrough.md",
    }

    for policy in _policy_specs():
        cfg = _variant_cfg(
            base, policy=policy, target=target, safety_buffer=safety_buffer
        )
        cfg.tags = dict(cfg.tags or {})
        cfg.tags["policy_name"] = policy["run_id"]
        result = run_cobertura(
            cfg, candles=candles, write_outputs=False, data_dir=DEFAULT_DATA_DIR
        )
        # Determinism check
        result2 = run_cobertura(
            CoberturaConfig.from_dict(cfg.to_dict()),
            candles=candles,
            write_outputs=False,
            data_dir=DEFAULT_DATA_DIR,
        )
        det_ok = (
            result.state == result2.state
            and len(result.fill_events) == len(result2.fill_events)
            and all(
                a.get("kind") == b.get("kind")
                and a.get("fill_price") == b.get("fill_price")
                and a.get("qty") == b.get("qty")
                for a, b in zip(result.fill_events, result2.fill_events)
            )
        )

        bundle = reconstruct_audit(
            policy=policy["run_id"], cfg=cfg, result=result
        )
        if not det_ok:
            bundle.invariant_violations.append(
                {
                    "policy": policy["run_id"],
                    "check": "determinism_rerun",
                    "detail": "second run mismatched fills/state",
                    "pass_fail": "FAIL",
                }
            )
            bundle.summary["audit_overall_pass"] = False
            bundle.summary["invariant_fail_count"] = (
                int(bundle.summary.get("invariant_fail_count") or 0) + 1
            )
        bundle.summary["determinism_pass"] = det_ok

        bundles.append(bundle)
        summaries.append(bundle.summary)
        all_lifecycle.extend(bundle.order_lifecycle)
        # fill ledger = enriched fills
        for i, f in enumerate(result.fill_events, 1):
            row = dict(f)
            row["policy"] = policy["run_id"]
            row["event_index"] = i
            all_fills.append(row)
        all_pos.extend(bundle.position_timeline)
        all_avg.extend(bundle.average_price_audit)
        all_pnl.extend(bundle.pnl_reconciliation)
        all_fee.extend(bundle.fee_reconciliation)
        all_trig.extend(bundle.trigger_timeline)
        all_tranche.extend(bundle.tranche_reconciliation)
        all_exit.extend(bundle.full_exit_audit)
        all_viol.extend(bundle.invariant_violations)
        all_amb.extend(bundle.ambiguous_intrabar_cases)
        all_shared.extend(bundle.shared_be_rounds)
        integrity["policies"][policy["run_id"]] = {
            "engine_integrity": result.integrity,
            "audit_summary": bundle.summary,
            "determinism_pass": det_ok,
        }

        _write_walkthrough(out / walk_names[policy["run_id"]], bundle)

    atomic_write_json(
        out / "config_snapshot.json",
        {
            **base.to_dict(),
            "audit": {
                "full_exit_target_mode": "net_be",
                "full_exit_target_usdt": target,
                "full_exit_safety_buffer_usdt": safety_buffer,
                "policies": [p["run_id"] for p in _policy_specs()],
            },
        },
    )
    atomic_write_json(out / "audit_summary.json", summaries)
    write_csv(out / "audit_summary.csv", summaries)
    write_csv(out / "order_lifecycle.csv", all_lifecycle)
    write_csv(out / "fill_ledger.csv", all_fills)
    write_csv(out / "position_timeline.csv", all_pos)
    write_csv(out / "average_price_audit.csv", all_avg)
    write_csv(out / "pnl_reconciliation.csv", all_pnl)
    write_csv(out / "fee_reconciliation.csv", all_fee)
    write_csv(out / "trigger_timeline.csv", all_trig)
    write_csv(out / "tranche_reconciliation.csv", all_tranche)
    write_csv(out / "full_exit_audit.csv", all_exit)
    write_csv(
        out / "invariant_violations.csv",
        all_viol
        or [
            {
                "policy": "",
                "check": "none",
                "detail": "no invariant violations",
                "pass_fail": "PASS",
            }
        ],
    )
    write_csv(out / "ambiguous_intrabar_cases.csv", all_amb)
    write_csv(out / "shared_be_rounds.csv", all_shared)
    atomic_write_json(out / "integrity.json", integrity)
    _write_report(out / "REPORT.md", summaries, bundles)

    manual = generate_manual_timelines(out)

    return {
        "output_dir": str(out),
        "summaries": summaries,
        "integrity": integrity,
        "manual_timelines": manual,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Cobertura full order audit + manual timelines")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: results/apt_full_order_audit_20260725)",
    )
    args = parser.parse_args(argv)
    payload = run_full_order_audit(output_dir=args.output_dir)
    print(f"Wrote {payload['output_dir']}")
    for s in payload["summaries"]:
        print(
            f"{s['policy']}: overall={'PASS' if s['audit_overall_pass'] else 'FAIL'} "
            f"status={s['final_status']} flat={s['flat_after_exit']} "
            f"inv_fail={s['invariant_fail_count']} fee_fail={s['fee_audit_fail_count']} "
            f"avg_fail={s['avg_audit_fail_count']} det={s.get('determinism_pass')}"
        )
    manual = payload.get("manual_timelines") or {}
    print(f"Manual timelines mismatches: {len(manual.get('mismatches') or [])}")
    for pol, meta in (manual.get("policies") or {}).items():
        print(
            f"  {pol}: timeline_events={meta['timeline_events']} "
            f"orders={meta['orders_created']} fills={meta['fills_only']} "
            f"audit_fills={meta['raw_audit_fills']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
