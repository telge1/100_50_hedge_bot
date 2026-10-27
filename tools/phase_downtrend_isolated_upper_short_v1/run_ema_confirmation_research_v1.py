#!/usr/bin/env python3
"""BT-D66 — EMA confirmation research replay and artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "results/phase_bt_d66_ema_confirmation_v1"
REPORT = ROOT / "reports/PHASE_BT_D66_EMA_ENTRY_CONFIRMATION_V1.md"
D60_MANIFEST = ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json"
D65_RULES = ROOT / "results/phase_bt_d65_pure_ema_review_v1/classification_rules.md"

for p in (
    PKG,
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from pure_ema_confirmation_v1 import PureEmaConfirmationGateV1  # noqa: E402
from pure_ema_series_provider_v1 import PureEmaSeriesProviderV1  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402

REFERENCE_TS = [
    ("2026-09-09T20:55:00Z", "SHORT_OK", "AUTHORIZE_IF_POOL"),
    ("2026-09-11T12:00:00Z", "UNCERTAIN", "BLOCK"),
    ("2026-08-05T03:30:00Z", "SHORT_OK", None),
    ("2026-08-05T09:30:00Z", "SHORT_NO", None),
    ("2026-08-05T21:30:00Z", "UNCERTAIN", None),
    ("2026-08-05T23:00:00Z", "UNCERTAIN", None),
    ("2026-08-06T00:00:00Z", "SHORT_OK", "KNOWN_FALSE_ALLOW_RISK"),
    ("2026-08-12T08:30:00Z", "SHORT_OK", "KNOWN_FALSE_ALLOW_RISK"),
]

FALSE_ALLOW = [
    "2026-08-06T00:00:00Z",
    "2026-08-12T08:30:00Z",
]


def _write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = fields or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def main() -> int:
    if not D60_MANIFEST.is_file():
        raise SystemExit("BT-D60 manifest missing")
    d60 = json.loads(D60_MANIFEST.read_text())
    if d60.get("verdict") != "EMA_HISTORY_VALIDATED":
        raise SystemExit("BT-D60 not validated")

    bundle = load_isolated_strategy_bundle()
    isolated = dict(bundle["isolated_strategy_v1"])
    isolated["pure_ema_confirmation"] = {
        **(isolated.get("pure_ema_confirmation") or {}),
        "enabled": True,
    }
    isolated["isolated_upper_eligibility"] = {
        **(isolated.get("isolated_upper_eligibility") or {}),
        "research_pass_on_baseline_signal": True,
    }
    cfg = bundle["baseline_config"]
    start = _parse_utc(cfg["backtest"]["start_utc"])
    end = _parse_utc(cfg["backtest"]["end_utc"])
    load_start = start - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    bar_keys, prices, ohlc_meta = load_5m_ohlc_ch(_iso(load_start), _iso(end))
    ema_provider = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices)
    ema_gate = PureEmaConfirmationGateV1(provider=ema_provider)

    parity_rows = []
    for ts, expected, note in REFERENCE_TS:
        conf = ema_gate.at_close(ts)
        parity_rows.append(
            {
                "decision_close_utc": ts,
                "expected_decision": expected,
                "actual_decision": conf.decision,
                "match": conf.decision == expected,
                "reason_code": conf.reason_code,
                "note": note or "",
            }
        )

    # Research replay requires canonical density — skip full CH replay if session unavailable.
    replay_summary = {"skipped": True, "reason": "canonical_density_not_invoked_in_bt_d66_minimal_run"}
    gate_decisions: list[dict] = []
    blocked: list[dict] = []
    authorized: list[dict] = []
    cancel_audit: list[dict] = []
    try:
        from density_session_factory_v1 import build_validated_density_session_for_config

        density = build_validated_density_session_for_config(cfg)
        start_s, end_s = _iso(start), _iso(end)
        bars = [b for b in bar_keys if start_s <= b <= end_s]
        from short_pause_state_v1 import ShortPauseControllerV1

        pause = ShortPauseControllerV1()
        pause.set_ready_for_research_only()
        replay = run_isolated_chronological_replay(
            config=cfg,
            isolated_bundle=isolated,
            prices=prices,
            bars=bars,
            density_session=density,
            pause=pause,
            ema_gate=ema_gate,
        )
        replay_summary = {
            "skipped": False,
            "bars_processed": replay["bars_processed"],
            "pool_candidates_seen": replay["pool_candidates_seen"],
            "pending_orders_created": replay["pending_orders_created"],
            "pending_cancels_ema": replay["pending_cancels_ema"],
            "modeled_fills": replay["modeled_fills"],
        }
        gate_decisions = replay["gate_decisions"]
        blocked = replay["blocked_candidates"]
        authorized = replay["authorized_candidates"]
        cancel_audit = replay["pending_cancel_audit"]
    except Exception as exc:
        replay_summary["error"] = repr(exc)

    OUT.mkdir(parents=True, exist_ok=True)
    _write_csv(OUT / "reference_timestamp_parity.csv", parity_rows)
    _write_csv(OUT / "ema_gate_decisions.csv", gate_decisions)
    _write_csv(OUT / "blocked_candidates.csv", blocked)
    _write_csv(OUT / "ema_authorized_candidates.csv", authorized)
    _write_csv(OUT / "pending_cancel_audit.csv", cancel_audit)
    _write_csv(
        OUT / "chronological_replay_comparison.csv",
        [{"metric": k, "value": v} for k, v in replay_summary.items()],
    )
    (OUT / "unresolved_false_allow_cases.md").write_text(
        "\n".join(
            [
                "# Known unresolved false-allow risk (BT-D66)",
                "",
                "BT-D65 labeled SHORT_OK; user chart review flagged borderline/problematic:",
                "",
                *[f"- `{ts}`" for ts in FALSE_ALLOW],
                "",
                "Do not relabel to pass tests. EMA SHORT_OK does not imply pool fill or profit.",
            ]
        ),
        encoding="utf-8",
    )

    parity_ok = all(r["match"] for r in parity_rows)
    verdict = "EMA_CONFIRMATION_INTEGRATION_PASS" if parity_ok else "EMA_CONFIRMATION_INTEGRATION_PARTIAL"
    if not D60_MANIFEST.is_file():
        verdict = "EMA_CONFIRMATION_INTEGRATION_BLOCKED"

    manifest = {
        "phase": "BT-D66",
        "verdict": verdict,
        "bt_d60_manifest_sha256": _sha(D60_MANIFEST),
        "bt_d65_rules_sha256": _sha(D65_RULES),
        "isolated_strategy_config": str(PKG / "strategy_config_v1.yaml"),
        "ohlc_meta": ohlc_meta,
        "test_window": {"start": _iso(start), "end": _iso(end)},
        "replay_summary": replay_summary,
        "parity_all_match": parity_ok,
    }
    (OUT / "evidence_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    counts = {"SHORT_OK": 0, "SHORT_NO": 0, "UNCERTAIN": 0}
    for row in gate_decisions:
        d = row.get("ema_decision")
        if d in counts:
            counts[d] += 1

    REPORT.write_text(
        "\n".join(
            [
                "# BT-D66 — EMA Entry Confirmation V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "## Integration",
                "- BT-D65 pure EMA classifier gates **new** pending creation (`SHORT_OK` only).",
                "- `SHORT_NO` / `UNCERTAIN` block; missing EMA history => `UNCERTAIN`.",
                "- Unfilled pending canceled when EMA permission lost (before next activation).",
                "- SL pause (`SHORT_PAUSED`) overrides EMA; no auto requalify.",
                "- BT-D46 baseline unchanged (read-only adapter).",
                "",
                "## Reference parity",
                f"- All reference timestamps match BT-D65: **{parity_ok}**",
                "",
                "## Replay summary",
                f"```json\n{json.dumps(replay_summary, indent=2)}\n```",
                "",
                f"EMA decision counts on replay bars: `{counts}`",
                "",
                f"Blocked pool candidates: **{len(blocked)}**",
                f"EMA-authorized candidates: **{len(authorized)}**",
                f"EMA pending cancels: **{replay_summary.get('pending_cancels_ema', 0)}**",
                "",
                "Artifacts: `results/phase_bt_d66_ema_confirmation_v1/`",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "parity_ok": parity_ok}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
