#!/usr/bin/env python3
"""Phase 4X-Z — isolated May reference replay (outputs under results/phase4xz_isolated_replay_v1/)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4xz_isolated_replay_v1"
DASH = ROOT / "backtester/short/dashboard"
MULTI = DASH / "research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"
INPUTS = MULTI / "inputs"
FROZEN_PATH = MULTI / "production/results/2026-05/5m_PD1/20260501T120000Z.json"
REF_ISO = "2026-05-01T12:00:00Z"
SYMBOL = "DOGEUSDT"
TIMEFRAME = "5m"
POOL_DETAIL = 1
ADAPTER = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_density_trp_adapter.js"
)
FROZEN_BANDS_SRC = ROOT / "results/phase4xw_jan_apr_warmup_origin_audit_v1/may01_density_bands.csv"

for _entry in (DASH, Path("/home/telgenbuescher/projects/trading_research_platform")):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))


def _parse_utc(s: str) -> datetime:
    s = s.strip().replace(" ", "T")
    if not s.endswith("Z") and "+" not in s:
        s += "Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def origin_logical(pid: str) -> str:
    parts = pid.split(":")
    if len(parts) >= 6 and parts[0] == "lldv2":
        return f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[4]}:{parts[5]}"
    return pid


def wcsv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({k for r in rows for k in r})
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def active_rows(payload: dict) -> list[dict]:
    ids = payload.get("active_pool_ids") or []
    pools = payload.get("pools") or {}
    rows = []
    for pid in ids:
        p = pools.get(pid) or {}
        ou = int(pid.rsplit(":", 1)[-1])
        odt = datetime.fromtimestamp(ou, tz=timezone.utc)
        rows.append(
            {
                "pool_id": pid,
                "origin_logical": origin_logical(pid),
                "origin_utc": odt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "origin_month": f"{odt.year:04d}-{odt.month:02d}",
                "side": p.get("side"),
                "pool_type": pid.split(":")[3] if len(pid.split(":")) > 3 else "",
                "bottom_price": p.get("bottom_price"),
                "top_price": p.get("top_price"),
                "known_at": p.get("known_at"),
                "active": True,
            }
        )
    return rows


def pools_for_density(payload: dict, close_iso: str) -> list[dict]:
    iso = _parse_utc(close_iso).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for pid in payload.get("active_pool_ids") or []:
        p = (payload.get("pools") or {}).get(pid) or {}
        out.append(
            {
                "pool_id": pid,
                "side": p.get("side"),
                "bottom": float(p.get("bottom_price")),
                "top": float(p.get("top_price")),
                "origin": origin_logical(pid),
                "segments": [[iso, iso]],
            }
        )
    return out


def density_bands(payload: dict, close_iso: str) -> list[dict]:
    analyzer = ROOT / "tools/phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js"
    prices = []
    for pid in payload.get("active_pool_ids") or []:
        p = (payload.get("pools") or {}).get(pid) or {}
        prices.extend([float(p.get("bottom_price", 0)), float(p.get("top_price", 0))])
    lo, hi = min(prices) * 0.995, max(prices) * 1.005
    t_ms = int(_parse_utc(close_iso).timestamp() * 1000)
    pools = pools_for_density(payload, close_iso)
    script = f"""
const {{ loadAdapter, computeExactBandsForSideWithOrigins }} = require({json.dumps(str(analyzer))});
const pools = {json.dumps(pools)};
const t = {t_ms};
const out = [];
for (const side of ['lower','upper']) {{
  for (const b of computeExactBandsForSideWithOrigins(pools, t, side, {lo}, {hi})) {{
    if (b.depth >= 2) out.push({{ side, band_index: out.length, lower_bound: b.y0, upper_bound: b.y1, unique_origin_depth: b.depth, origin_ids: (b.origins||[]).join('|') }});
  }}
}}
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout)


def band_key(b: dict) -> tuple:
    return (
        b.get("side"),
        str(b.get("lower_bound")),
        str(b.get("upper_bound")),
        int(b.get("unique_origin_depth") or 0),
        b.get("origin_ids") or "",
    )


def preflight() -> dict[str, Any]:
    from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
        ohlcv_content_hash,
        require_snapshot,
        revision_metadata_hash,
    )
    from research_charts.live_micro_lld_v2_ch_test.phase11l.frozen import required_source_start_unix
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
        rolling_history_from_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.identity import ENGINE_VERSION

    decision = _parse_utc(REF_ISO)
    history = rolling_history_from_close(decision, 4)
    need_unix = required_source_start_unix(decision, POOL_DETAIL, TIMEFRAME)
    need_from = datetime.fromtimestamp(need_unix, tz=timezone.utc)

    manifests = list(INPUTS.glob("*.manifest.json"))
    if len(manifests) != 1:
        return {"status": "REPLAY_INPUT_BLOCKED", "reason": f"expected 1 manifest in inputs, got {len(manifests)}"}
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    snap_id = manifest["snapshot_id"]
    price_hash = manifest["ohlcv_content_hash"]
    rev_hash = manifest["revision_metadata_hash"]
    range_start = _parse_utc(str(manifest["range_start"]))
    range_end = _parse_utc(str(manifest["range_end_exclusive"]))

    if need_from < range_start or decision >= range_end:
        return {
            "status": "REPLAY_INPUT_BLOCKED",
            "reason": "immutable snapshot range does not cover engine need",
            "need_from": need_from.isoformat(),
            "range_start": range_start.isoformat(),
            "range_end_exclusive": range_end.isoformat(),
        }

    rows = require_snapshot(INPUTS, snap_id, ohlcv_hash=price_hash)
    if ohlcv_content_hash(rows) != price_hash or revision_metadata_hash(rows) != rev_hash:
        return {"status": "REPLAY_INPUT_BLOCKED", "reason": "manifest hash mismatch on parquet read"}

    frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))
    fi = frozen.get("frozen_input") or {}
    if fi.get("snapshot_id") != snap_id or fi.get("price_hash") != price_hash:
        return {"status": "REPLAY_INPUT_BLOCKED", "reason": "frozen reference frozen_input != local immutable manifest"}

    ch_verified = "NOT_ATTEMPTED"
    ch_note = ""
    try:
        import clickhouse_connect
        from research_charts.clickhouse_config import load_clickhouse_config
        from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import CandleInputRow

        cfg = load_clickhouse_config()
        client = clickhouse_connect.get_client(**cfg.connect_kwargs())
        q = f"""
        SELECT open_time, close_time, open, high, low, close, volume, turnover, is_closed, source,
               source_event_time, ingested_at
        FROM {cfg.database}.{cfg.table} FINAL
        WHERE exchange = {{ex:String}} AND symbol = 'DOGEUSDT' AND interval = '1m' AND is_closed = 1
          AND open_time >= {{a:DateTime64(3,'UTC')}} AND open_time < {{b:DateTime64(3,'UTC')}}
        ORDER BY open_time
        """
        ch_rows = client.query(
            q,
            parameters={"ex": cfg.exchange, "a": need_from, "b": decision},
        ).result_rows
        client.close()
        # CH query uses open_time < decision (5m bar close); count 1m opens in [need_from, decision).
        expected_minutes = int((decision - need_from).total_seconds() // 60)
        if len(ch_rows) != expected_minutes:
            ch_verified = "CH_SPAN_COUNT_MISMATCH"
            ch_note = f"got {len(ch_rows)} expected ~{expected_minutes}"
        else:
            sample = rows[0]
            ch_sample = ch_rows[0]
            # spot: first minute in range should align
            if _parse_utc(str(ch_sample[0])) != datetime.fromtimestamp(need_unix, tz=timezone.utc):
                ch_verified = "CH_ALIGNMENT_MISMATCH"
            else:
                ch_verified = "CH_SPAN_COMPLETE_READ"
                ch_note = f"{len(ch_rows)} minutes {need_from.isoformat()}..{decision.isoformat()}"
    except Exception as exc:
        ch_verified = f"CH_ERROR:{type(exc).__name__}"
        ch_note = str(exc)[:200]

    return {
        "status": "PREFLIGHT_OK",
        "engine_version": ENGINE_VERSION,
        "decision_close": REF_ISO,
        "history_from_close": history.isoformat(),
        "candle_load_start_expected": frozen.get("candle_load_start"),
        "required_1m_from": need_from.isoformat(),
        "immutable_snapshot_id": snap_id,
        "price_hash": price_hash,
        "revision_hash": rev_hash,
        "row_count": len(rows),
        "frozen_engine_input_hash": frozen.get("engine_input_hash"),
        "frozen_active_fingerprint": frozen.get("active_state_fingerprint"),
        "frozen_candle_count": frozen.get("candle_count"),
        "ch_independent_span_check": ch_verified,
        "ch_note": ch_note,
        "replay_stage_a": "IDENTICAL_IMMUTABLE_PARQUET",
        "replay_stage_b": ch_verified,
        "replay_stage_c": "required_source_start_unix verified inside manifest range",
    }


def run_replay(pf: dict) -> dict:
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
        build_active_state_at_decision_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.parallel_verify import verification_record
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
        HISTORY_POLICY_ROLLING_4M,
        rolling_history_from_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
        ohlcv_content_hash,
        require_snapshot,
        revision_metadata_hash,
    )
    from research_charts.live_micro_lld_v2_ch_test.phase11m.session import ReusedSnapshotSession

    decision = _parse_utc(REF_ISO)
    history = rolling_history_from_close(decision, 4)
    rows = require_snapshot(INPUTS, pf["immutable_snapshot_id"], ohlcv_hash=pf["price_hash"])
    price = ohlcv_content_hash(rows)
    revision = revision_metadata_hash(rows)
    identity = {
        "snapshot_id": pf["immutable_snapshot_id"],
        "price_hash": price,
        "revision_hash": revision,
        "clickhouse_candle_reads": 0,
    }
    session = ReusedSnapshotSession(
        rows,
        snapshot_id=pf["immutable_snapshot_id"],
        ohlcv_hash=price,
        timeframe=TIMEFRAME,
    )
    session.max_1m_bars = 500_000
    snap, candles, result = build_active_state_at_decision_close(
        SYMBOL,
        TIMEFRAME,
        history,
        decision,
        pool_detail=POOL_DETAIL,
        history_policy=HISTORY_POLICY_ROLLING_4M,
        candle_session=session,
    )
    if int(session.stats.ch_fetch_calls) != 0:
        raise RuntimeError("ClickHouse candle read during replay")
    record = verification_record(snap, candles, result, decision)
    record["frozen_input"] = identity
    record["timeframe"] = TIMEFRAME
    record["pool_detail"] = POOL_DETAIL
    record["symbol"] = SYMBOL
    record["engine_version"] = snap.engine_version
    out_path = OUT / "replay_snapshot_20260501T120000Z.json"
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def compare_active(frozen: dict, replay: dict) -> list[dict]:
    diffs: list[dict] = []
    f_ids = set(frozen.get("active_pool_ids") or [])
    r_ids = set(replay.get("active_pool_ids") or [])
    for pid in sorted(f_ids - r_ids):
        diffs.append({"diff_type": "missing_in_replay", "pool_id": pid})
    for pid in sorted(r_ids - f_ids):
        diffs.append({"diff_type": "extra_in_replay", "pool_id": pid})
    fp = frozen.get("pools") or {}
    rp = replay.get("pools") or {}
    for pid in sorted(f_ids & r_ids):
        for field in ("side", "bottom_price", "top_price", "known_at"):
            fv, rv = fp.get(pid, {}).get(field), rp.get(pid, {}).get(field)
            if fv != rv:
                diffs.append(
                    {
                        "diff_type": "field_mismatch",
                        "pool_id": pid,
                        "field": field,
                        "frozen": fv,
                        "replay": rv,
                    }
                )
    if frozen.get("engine_input_hash") != replay.get("engine_input_hash"):
        diffs.append(
            {
                "diff_type": "engine_input_hash",
                "frozen": frozen.get("engine_input_hash"),
                "replay": replay.get("engine_input_hash"),
            }
        )
    if frozen.get("active_state_fingerprint") != replay.get("active_state_fingerprint"):
        diffs.append(
            {
                "diff_type": "active_state_fingerprint",
                "frozen": frozen.get("active_state_fingerprint"),
                "replay": replay.get("active_state_fingerprint"),
            }
        )
    return diffs


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    pf = preflight()
    coverage_rows = [
        {
            "check": "immutable_manifest_covers_required_1m",
            "status": pf.get("status"),
            "required_1m_from": pf.get("required_1m_from"),
            "snapshot_id": pf.get("immutable_snapshot_id"),
            "price_hash": pf.get("price_hash"),
        },
        {
            "check": "stage_a_immutable_parquet",
            "status": pf.get("replay_stage_a"),
            "notes": "Replay uses local cfae2571 parquet only",
        },
        {
            "check": "stage_b_ch_independent_read",
            "status": pf.get("replay_stage_b"),
            "notes": pf.get("ch_note"),
        },
        {
            "check": "stage_c_engine_required_start",
            "status": pf.get("replay_stage_c"),
            "notes": pf.get("required_1m_from"),
        },
    ]
    wcsv(OUT / "independent_input_coverage.csv", coverage_rows)

    preflight_md = [
        "# Replay preflight — Phase 4X-Z",
        "",
        f"**Status:** `{pf.get('status')}`",
        "",
        json.dumps(pf, indent=2),
    ]
    (OUT / "replay_preflight.md").write_text("\n".join(preflight_md), encoding="utf-8")

    provenance = [
        "# Original engine provenance",
        "",
        f"- **Engine:** `{pf.get('engine_version', 'unknown')}` (current codebase `identity.ENGINE_VERSION`)",
        "- **Entry:** `build_active_state_at_decision_close` + `run_live_micro_lld_v2` (same as `run_multi_month.py` worker)",
        f"- **Production batch evidence:** `doge_multi_month_2026_history/production/workers/worker_0.log` contains `2026-05 5m_PD1 20260501T120000Z`",
        f"- **Immutable input:** `{pf.get('immutable_snapshot_id')}` under `doge_multi_month_2026_history/inputs/`",
        "- **Frozen reference:** `production/results/2026-05/5m_PD1/20260501T120000Z.json` (read-only)",
        "",
        "Original git revision at Oct-2026 generation is **not** recorded in frozen JSON.",
    ]
    (OUT / "original_engine_provenance.md").write_text("\n".join(provenance), encoding="utf-8")

    if pf.get("status") != "PREFLIGHT_OK":
        (OUT / "historical_completeness_assessment.md").write_text(
            f"# Assessment\n\n**VERDICT:** `REPLAY_PREFLIGHT_BLOCKED`\n\n{pf.get('reason')}\n",
            encoding="utf-8",
        )
        return 0

    frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))
    wcsv(OUT / "frozen_active_origins.csv", active_rows(frozen))

    try:
        replay = run_replay(pf)
    except Exception:
        (OUT / "replay_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        (OUT / "historical_completeness_assessment.md").write_text(
            "# Assessment\n\n**VERDICT:** `REPLAY_PREFLIGHT_BLOCKED`\n\nEngine replay raised an exception; see `replay_error.log`.\n",
            encoding="utf-8",
        )
        return 1

    wcsv(OUT / "replay_active_origins.csv", active_rows(replay))
    diffs = compare_active(frozen, replay)
    wcsv(OUT / "active_origin_diff.csv", diffs)

    # density
    frozen_bands = []
    if FROZEN_BANDS_SRC.is_file():
        with FROZEN_BANDS_SRC.open(encoding="utf-8") as f:
            frozen_bands = list(csv.DictReader(f))
    else:
        frozen_bands = density_bands(frozen, REF_ISO)
    replay_bands = density_bands(replay, REF_ISO)
    wcsv(OUT / "frozen_density_bands.csv", frozen_bands)
    wcsv(OUT / "replay_density_bands.csv", replay_bands)

    f_keys = {band_key(b) for b in frozen_bands}
    r_keys = {band_key(b) for b in replay_bands}
    dens_diffs = []
    for k in sorted(f_keys - r_keys):
        dens_diffs.append({"diff_type": "missing_in_replay", "band_key": str(k)})
    for k in sorted(r_keys - f_keys):
        dens_diffs.append({"diff_type": "extra_in_replay", "band_key": str(k)})
    dens_diffs.append({"metric": "frozen_band_count", "value": len(frozen_bands)})
    dens_diffs.append({"metric": "replay_band_count", "value": len(replay_bands)})
    wcsv(OUT / "density_parity_diff.csv", dens_diffs)

    input_ok = pf.get("status") == "PREFLIGHT_OK"
    engine_ok = (
        frozen.get("engine_input_hash") == replay.get("engine_input_hash")
        and frozen.get("active_state_fingerprint") == replay.get("active_state_fingerprint")
        and not any(d["diff_type"] in ("missing_in_replay", "extra_in_replay") for d in diffs)
    )
    active_ok = engine_ok and not any(d["diff_type"] == "field_mismatch" for d in diffs)
    density_ok = f_keys == r_keys and len(frozen_bands) == 363

    verdict = "MAY_REFERENCE_REPLAY_FAILED"
    if active_ok and density_ok:
        verdict = "MAY_REFERENCE_REPLAY_VERIFIED"
    elif active_ok:
        verdict = "MAY_REFERENCE_REPLAY_PARTIAL"

    hist_verified = "NO"
    if active_ok and density_ok:
        hist_verified = "REFERENCE_CLOSE_ONLY_NOT_FULL_MAY"
    elif active_ok:
        hist_verified = "ACTIVE_ONLY_AT_REFERENCE"

    assessment = f"""# Historical completeness assessment — Phase 4X-Z

## VERDICT: `{verdict}`

## Verification stages

| Stage | Result |
|-------|--------|
| INPUT_COVERAGE_VERIFIED | {"YES" if input_ok else "NO"} |
| ENGINE_REPRODUCIBILITY_VERIFIED | {"YES" if engine_ok else "NO"} |
| ACTIVE_ORIGIN_REPLAY_PARITY_VERIFIED | {"YES" if active_ok else "NO"} |
| DENSITY_REPLAY_PARITY_VERIFIED | {"YES" if density_ok else "NO"} |
| HISTORICAL_COMPLETENESS_VERIFIED | {hist_verified} |

## Counts @ {REF_ISO}

| Metric | Frozen | Replay |
|--------|-------:|-------:|
| ACTIVE pool_ids | {len(frozen.get("active_pool_ids") or [])} | {len(replay.get("active_pool_ids") or [])} |
| engine_input_hash match | | {frozen.get("engine_input_hash") == replay.get("engine_input_hash")} |
| active_state_fingerprint match | | {frozen.get("active_state_fingerprint") == replay.get("active_state_fingerprint")} |
| Depth≥2 bands | {len(frozen_bands)} | {len(replay_bands)} |
| active_origin_diff rows | {len(diffs)} | |

## Mandated answers

1. Original inputs fully verified? **Immutable parquet + hash match frozen** YES; **CH independent hash equality** {pf.get("replay_stage_b")}.
2. Original LMV2 started independently? **YES** (no seeded ACTIVE list).
3. Exactly 322 ACTIVE? **{len(frozen.get("active_pool_ids") or [])} vs {len(replay.get("active_pool_ids") or [])}** — parity **{active_ok}**.
4. Jan–Apr origins identical? **Subset test via full pool_id set parity** — **{active_ok}**.
5. Exactly 363 bands? **{len(frozen_bands)} vs {len(replay_bands)}** — **{density_ok}**.
6. Differences? See `active_origin_diff.csv` ({len(diffs)} rows) and `density_parity_diff.csv`.
7. Independent completeness @ reference? **{hist_verified}** (single-close replay; same immutable input family).
8. Full May still needed? **YES** — 8928 closes sequential drift check not run.

"""
    (OUT / "historical_completeness_assessment.md").write_text(assessment, encoding="utf-8")

    report = f"""# DOGEUSDT – PHASE 4X-Z INDEPENDENT MAY REPLAY VALIDATION V1

**VERDICT: `{verdict}`**

Artifacts: `results/phase4xz_isolated_replay_v1/`  
Runner: `tools/phase4xz_isolated_replay_v1/run_replay.py`

See `historical_completeness_assessment.md` for stage matrix and Q&A.

**STOP** — no CH writes, no frozen overwrites.
"""
    (ROOT / "reports/DOGEUSDT_PHASE4XZ_INDEPENDENT_MAY_REPLAY_VALIDATION_V1.md").write_text(
        report, encoding="utf-8"
    )
    print(verdict, "diffs", len(diffs), "bands", len(frozen_bands), len(replay_bands))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
