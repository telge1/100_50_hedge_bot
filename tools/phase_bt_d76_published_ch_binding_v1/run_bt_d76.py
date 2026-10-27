#!/usr/bin/env python3
"""BT-D76 — published CH density binding: parity, timing, mini replay."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "results/phase_bt_d76_published_ch_binding_v1"
REPORT = ROOT / "reports/PHASE_BT_D76_PUBLISHED_CH_BACKTESTER_BINDING_V1.md"

TOOLS = (
    PKG,
    ROOT / "tools/phase_bt_d3_canonical_depth2_provider_v1",
    ROOT / "tools/phase_bt_d4_enforced_depth2_backtester_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_downtrend_isolated_upper_short_v1",
    ROOT / "tools/phase_bt_d67_canonical_ema_replay_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
)
for p in TOOLS:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from parity_compare_v1 import compare_band_sets  # noqa: E402
from density_provider_v1 import get_density_bands  # noqa: E402
from research_density_gateway_v1 import snapshot_payload  # noqa: E402

from published_ch_density_provider_v1 import get_density_bands_published_clickhouse  # noqa: E402

PARITY_CLOSES = [
    "2026-05-15T12:00:00Z",
    "2026-08-01T19:00:00Z",
    "2026-09-09T20:55:00Z",
    "2026-09-23T12:00:00Z",
    "2026-09-25T11:00:00Z",
]

MINI_REPLAY = {
    "start_utc": "2026-09-09T20:50:00Z",
    "end_utc": "2026-09-09T21:10:00Z",
    "warmup_note": "Uses May–Sep CH publish; April 5m PD1 gap documented in report",
}


def _bands_for_parity(snap: dict[str, Any]) -> list[dict[str, Any]]:
    return list(snap.get("upper_bands") or []) + list(snap.get("lower_bands") or [])


def run_density_parity() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    all_ok = True
    member_cache: dict[str, list[dict]] = {}
    for close in PARITY_CLOSES:
        use_fixture = close == "2026-09-25T11:00:00Z"
        row: dict[str, Any] = {"decision_close_utc": close}
        try:
            t0 = time.perf_counter()
            canon = get_density_bands(
                "DOGEUSDT",
                close,
                use_parity_fixture_window=use_fixture,
            )
            canon_s = time.perf_counter() - t0
            t1 = time.perf_counter()
            pub = get_density_bands_published_clickhouse(
                "DOGEUSDT",
                close,
                use_parity_fixture_window=use_fixture,
                member_cache=member_cache,
            )
            pub_s = time.perf_counter() - t1
            cs = snapshot_payload(canon)
            ps = snapshot_payload(pub)
            cmp = compare_band_sets(_bands_for_parity(cs), _bands_for_parity(ps))
            row.update(
                {
                    "parity_ok": cmp["ok"],
                    "canonical_s": round(canon_s, 4),
                    "published_ch_s": round(pub_s, 4),
                    "speedup_x": round(canon_s / pub_s, 2) if pub_s > 0 else None,
                    "band_compare": cmp,
                    "fingerprint_canon": canon.manifest.get("manifest_hash"),
                    "fingerprint_pub": pub.manifest.get("manifest_hash"),
                }
            )
            if not cmp["ok"]:
                all_ok = False
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            row["parity_ok"] = False
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    return {"ok": all_ok, "closes": rows, "member_cache_keys": len(member_cache)}


def run_timing_warm() -> dict[str, Any]:
    """Second pass on Sep-9 sample (warm member cache)."""
    close = "2026-09-09T20:55:00Z"
    cache: dict[str, list[dict]] = {}
    get_density_bands_published_clickhouse("DOGEUSDT", close, member_cache=cache)
    t0 = time.perf_counter()
    get_density_bands_published_clickhouse("DOGEUSDT", close, member_cache=cache)
    warm_pub = time.perf_counter() - t0
    t1 = time.perf_counter()
    get_density_bands("DOGEUSDT", close)
    cold_canon = time.perf_counter() - t1
    cache_read_s = None
    bundle = ROOT / "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle"
    manifest_path = bundle / "manifest.json"
    if bundle.is_dir() and manifest_path.is_file():
        from density_bridge_v1 import create_density_session  # noqa: E402
        from config_loader_v1 import load_validated_baseline_config  # noqa: E402

        cfg = copy.deepcopy(load_validated_baseline_config())
        cfg["density"]["density_source"] = "validated_cache"
        cfg["density"]["validated_cache"] = {"bundle_path": str(bundle.resolve())}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        closes = manifest.get("decision_closes") or []
        if close in closes:
            sess = create_density_session(cfg)
            t2 = time.perf_counter()
            sess.collect_at_close(close)
            cache_read_s = time.perf_counter() - t2
    return {
        "close": close,
        "canonical_cold_s": round(cold_canon, 4),
        "published_ch_warm_s": round(warm_pub, 4),
        "validated_cache_read_s": round(cache_read_s, 6) if cache_read_s is not None else None,
    }


def run_mini_replay() -> dict[str, Any]:
    from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
    from density_bridge_v1 import create_density_session  # noqa: E402
    from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
    from ohlc_loader_v1 import load_5m_ohlc_ch  # noqa: E402
    from pure_ema_confirmation_v1 import build_pure_ema_gate  # noqa: E402
    from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402

    bundle = load_isolated_strategy_bundle()
    config = copy.deepcopy(bundle["baseline_config"])
    isolated = dict(bundle["isolated_strategy_v1"])
    isolated["pure_ema_confirmation"] = {
        **(isolated.get("pure_ema_confirmation") or {}),
        "enabled": True,
        "classifier_version": "v3",
    }

    cfg_canon = copy.deepcopy(config)
    cfg_canon["density"]["density_source"] = "canonical_provider"
    cfg_pub = copy.deepcopy(config)
    cfg_pub["density"]["density_source"] = "published_clickhouse"
    cfg_pub["density"]["published_clickhouse"] = {}

    start = MINI_REPLAY["start_utc"]
    end = MINI_REPLAY["end_utc"]
    bar_keys, prices, _meta = load_5m_ohlc_ch(start, end)
    bars = [b for b in bar_keys if start <= b <= end]
    gate = build_pure_ema_gate(bar_keys=bar_keys, prices=prices, isolated_strategy_config=isolated)

    def _digest(run: dict[str, Any]) -> str:
        payload = {
            "pending": run.get("pending_summary"),
            "fills": run.get("modeled_fills"),
            "positions": run.get("modeled_positions"),
            "authorized": len(run.get("authorized_candidates") or []),
            "blocked": len(run.get("blocked_candidates") or []),
        }
        return json.dumps(payload, sort_keys=True)

    def _run(cfg: dict[str, Any]) -> dict[str, Any]:
        pause = ShortPauseControllerV1()
        pause.set_ready_for_research_only()
        return run_isolated_chronological_replay(
            config=cfg,
            isolated_bundle=isolated,
            prices=prices,
            bars=bars,
            density_session=create_density_session(cfg),
            pause=pause,
            ema_gate=gate,
        )

    r_canon = _run(cfg_canon)
    r_pub = _run(cfg_pub)
    d_canon = _digest(r_canon)
    d_pub = _digest(r_pub)
    return {
        "bars": len(bars),
        "start_utc": start,
        "end_utc": end,
        "chronology_match": d_canon == d_pub,
        "canonical_digest": d_canon[:64],
        "published_digest": d_pub[:64],
    }


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def write_report(parity: dict[str, Any], timing: dict[str, Any], replay: dict[str, Any] | None) -> str:
    blocked = not parity.get("ok")
    if blocked:
        verdict = "PUBLISHED_CH_FAST_PATH_BLOCKED"
    elif replay and not replay.get("chronology_match"):
        verdict = "PUBLISHED_CH_FAST_PATH_BLOCKED"
    else:
        verdict = "PUBLISHED_CH_FAST_PATH_PASS"
    lines = [
        "# BT-D76 — Published ClickHouse Backtester Binding V1",
        "",
        f"**VERDICT:** `{verdict}`",
        "",
        f"**Git:** `{_git_head()}`",
        "",
        "## Components reused",
        "",
        "- `density_bridge_v1.create_density_session` → `published_clickhouse`",
        "- `PublishedActiveReader` / `load_historical_pool_state_at_close` (5m via 4X-AO import path)",
        "- `build_landscape_window(..., density_only=True, member_cache=...)` (pools dashboard)",
        "- `analyzer_depth2_landscape.js` via `node_bridge_v1`",
        "- D8 `validated_cache` timing reference when bundle covers sample close",
        "",
        "## Parity (canonical vs published_clickhouse)",
        "",
        "```json",
        json.dumps(parity, indent=2),
        "```",
        "",
        "## Timing",
        "",
        "```json",
        json.dumps(timing, indent=2),
        "```",
        "",
    ]
    if replay:
        lines.extend(
            [
                "## Mini chronological replay (EMA V3)",
                "",
                "```json",
                json.dumps(replay, indent=2),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Coverage limits",
            "",
            "- **April 2026:** no 5m PD1 publish on CH pipeline; 168h warmup before May 1 needs non-CH sources.",
            "- Parity samples are bounded; not full May–Sep proof.",
            "- Default `strategy_config_v1.yaml` remains `canonical_provider` (fail-closed).",
            "",
            "## May–Sep fast backtest readiness",
            "",
            (
                "Published-CH session is ready for **research opt-in** on validated publish range "
                "(May–Sep 2026 5m PD1) when parity gate passes."
                if verdict == "PUBLISHED_CH_FAST_PATH_PASS"
                else "Blocked until parity/replay gates pass."
            ),
            "",
        ]
    )
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    return verdict


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    parity = run_density_parity()
    (OUT / "parity_results.json").write_text(json.dumps(parity, indent=2), encoding="utf-8")
    if not parity.get("ok"):
        timing = run_timing_warm()
        (OUT / "timing.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
        write_report(parity, timing, None)
        print("BLOCKED: density parity failed")
        return 2
    timing = run_timing_warm()
    (OUT / "timing.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
    replay = run_mini_replay()
    (OUT / "mini_replay.json").write_text(json.dumps(replay, indent=2), encoding="utf-8")
    verdict = write_report(parity, timing, replay)
    print(verdict)
    return 0 if verdict == "PUBLISHED_CH_FAST_PATH_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
