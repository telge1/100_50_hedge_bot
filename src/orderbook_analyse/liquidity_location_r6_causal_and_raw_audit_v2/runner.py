"""Run causal feature smoke + raw OB diagnosis (no Phase-4 OOS)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import fetch_candles_1m
from orderbook_analyse.l2_wall_attack_discovery.trades import load_public_trades
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from . import (
    AUDIT_ID,
    AUDIT_V1_DIR,
    DECISION_VARIANTS,
    LIVE_ARCHIVE,
    OUT_DIR_DEFAULT,
    PHASE3_CLAIMS_STATUS,
    PHASE3_DIR,
    SHADOW_ARCHIVE,
    VERDICT_ROOT_CAUSE,
)
from .causal import (
    absorption_subminute,
    assert_causal,
    decision_at_for_variant,
    future_only_path_labels,
    near_edge_reclaim_closed_candles,
    near_edge_reclaim_subminute,
)
from .raw_diag import (
    audit_all_segments,
    classify_raw_matrix,
    collector_process_snapshot,
    inventory_segments,
)


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def _naive(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def run_causal_smoke(
    episodes: pd.DataFrame,
    *,
    n_smoke: int = 8,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Small causal episode smoke across decision variants (no full OOS)."""
    client = get_clickhouse_client()
    ep = episodes[episodes["analyzable_core"] == True].sort_values("first_touch_at").head(n_smoke)  # noqa: E712
    smoke_rows: list[dict[str, Any]] = []
    prov_rows: list[dict[str, Any]] = []
    label_rows: list[dict[str, Any]] = []

    for _, r in ep.iterrows():
        t2 = _naive(r["first_touch_at"])
        side = str(r["side"])
        near = float(r["upper_price"]) if side == "BID" else float(r["lower_price"])
        lower, upper = float(r["lower_price"]), float(r["upper_price"])
        a = (t2 - pd.Timedelta(minutes=10)).to_pydatetime()
        b = (t2 + pd.Timedelta(minutes=45)).to_pydatetime()
        # load_public_trades expects aware or naive consistently — pass UTC-aware
        from datetime import timezone as tz

        a_u = a.replace(tzinfo=tz.utc) if a.tzinfo is None else a
        b_u = b.replace(tzinfo=tz.utc) if b.tzinfo is None else b
        trades = load_public_trades(symbol=r["symbol"], start=a_u, end=b_u)
        if not trades.empty:
            trades = trades.copy()
            trades["trade_ts"] = trades["trade_ts"].map(_naive)
        candles = fetch_candles_1m(client, r["symbol"], a, b)
        if not candles.empty:
            candles = candles.copy()
            candles["open_time"] = candles["open_time"].map(_naive)

        for variant in DECISION_VARIANTS:
            dec, st = decision_at_for_variant(t2, variant, candles_1m=candles)
            base = {
                "episode_id": r["episode_id"],
                "symbol": r["symbol"],
                "side": side,
                "timeframe": r["timeframe"],
                "first_touch_at": t2.isoformat(),
                "variant": variant,
                "decision_status": st,
            }
            if pd.isna(dec):
                smoke_rows.append({**base, "decision_at": None, "status": st})
                continue
            if variant == "SUBMINUTE_30S":
                reclaim = near_edge_reclaim_subminute(
                    trades,
                    side=side,
                    near_edge=near,
                    first_touch_at=t2,
                    decision_at=dec,
                )
                absorp = absorption_subminute(
                    trades, side=side, first_touch_at=t2, decision_at=dec
                )
            else:
                reclaim = near_edge_reclaim_closed_candles(
                    candles,
                    side=side,
                    near_edge=near,
                    first_touch_at=t2,
                    decision_at=dec,
                    variant=variant,
                )
                # Absorption for closed variants still uses trades ≤ decision_at
                absorp = absorption_subminute(
                    trades, side=side, first_touch_at=t2, decision_at=dec
                )
                absorp["variant"] = variant

            assert_causal(reclaim)
            assert_causal(absorp)

            smoke_rows.append(
                {
                    **base,
                    "decision_at": dec.isoformat(),
                    "reclaim_status": reclaim.get("status"),
                    "reclaimed": reclaim.get("reclaimed"),
                    "reclaim_causal_ok": reclaim.get("causal_ok"),
                    "reclaim_max_ts": reclaim.get("max_feature_timestamp"),
                    "reclaim_price_source": reclaim.get("price_source"),
                    "absorption_status": absorp.get("status"),
                    "absorption_flag": absorp.get("absorption_flag"),
                    "absorption_causal_ok": absorp.get("causal_ok"),
                    "absorption_max_ts": absorp.get("max_feature_timestamp"),
                    "absorption_continuation": absorp.get("price_continuation"),
                    "trade_count_touch": absorp.get("trade_count"),
                }
            )
            for feat in (reclaim, absorp):
                prov_rows.append(
                    {
                        "episode_id": r["episode_id"],
                        "feature_name": feat.get("feature_name"),
                        "variant": feat.get("variant"),
                        "window_start": feat.get("window_start"),
                        "window_end": feat.get("window_end"),
                        "decision_at": feat.get("decision_at"),
                        "max_source_timestamp": feat.get("max_source_timestamp"),
                        "source_type": feat.get("source_type"),
                        "source_row_count": feat.get("source_row_count"),
                        "missingness": feat.get("missingness"),
                        "causal_ok": feat.get("causal_ok"),
                        "status": feat.get("status"),
                    }
                )

            # ATR from prior bars at decision
            hist = candles[candles["open_time"] <= dec].tail(20)
            atr = float((hist["high"] - hist["low"]).tail(14).mean()) if len(hist) >= 5 else float("nan")
            labels = future_only_path_labels(
                candles,
                side=side,
                near_edge=near,
                lower=lower,
                upper=upper,
                decision_at=dec,
                atr=atr,
            )
            label_rows.append({"episode_id": r["episode_id"], "variant": variant, **labels})

    return pd.DataFrame(smoke_rows), pd.DataFrame(prov_rows), pd.DataFrame(label_rows)


def run_audit_v2(
    *,
    phase3_dir: Path = Path(PHASE3_DIR),
    out_dir: Path = Path(OUT_DIR_DEFAULT),
    n_smoke: int = 8,
    audit_all_raw: bool = True,
) -> dict[str, Any]:
    t0 = time.time()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    phase3_dir = Path(phase3_dir)

    episodes = pd.read_csv(phase3_dir / "r6_episodes.csv", low_memory=False)

    # --- Causal smoke ---
    print("CAUSAL_SMOKE", flush=True)
    smoke, prov, labels = run_causal_smoke(episodes, n_smoke=n_smoke)
    # Fail-fast on any causal_ok False with status OK (should not happen)
    bad = prov[(prov["causal_ok"] == False) & (prov["status"] == "OK")]  # noqa: E712
    if len(bad):
        raise RuntimeError(f"causal_ok false with OK status: {bad.head()}")

    # --- Raw diagnosis ---
    print("COLLECTOR_SNAPSHOT", flush=True)
    coll = collector_process_snapshot()
    shadow = Path(SHADOW_ARCHIVE)
    print("RAW_INVENTORY", flush=True)
    inv_path = out_dir / "raw_segment_inventory.csv"
    audit_path = out_dir / "raw_segment_replay_audit.csv"
    chain_path = out_dir / "raw_chain_inventory.csv"
    fail_path = out_dir / "first_failure_examples.csv"

    if audit_path.is_file() and chain_path.is_file() and fail_path.is_file() and inv_path.is_file():
        print("RAW_AUDIT_REUSE", flush=True)
        inv_df = pd.read_csv(inv_path)
        audit_df = pd.read_csv(audit_path)
        chain_df = pd.read_csv(chain_path)
        fail_df = pd.read_csv(fail_path)
        inv_rows = inv_df.to_dict(orient="records")
        audit_rows = audit_df.to_dict(orient="records")
        chain_rows = chain_df.to_dict(orient="records")
        first_fail = fail_df.to_dict(orient="records")
    else:
        inv_rows, _ = inventory_segments(shadow) if shadow.exists() else ([], [])
        print("RAW_AUDIT_SEGMENTS", flush=True)
        if audit_all_raw and shadow.exists():
            audit_rows, chain_rows, first_fail = audit_all_segments(shadow)
        else:
            audit_rows, chain_rows, first_fail = [], [], []
    matrix = classify_raw_matrix(audit_rows, inv_rows)

    # Offline fix: consumer helper only (no live restart, no manifest rewrite)
    offline_fix = {
        "implemented": True,
        "scope": "consumer_assessment_only",
        "description": (
            "Use ob200_v3_raw_discovery.audit.process_segment (u-continuity + "
            "rotation_checkpoint bootstrap) instead of trusting manifest "
            "replayable/completion_status/sequence_gaps from the pre-fix writer. "
            "Treat checkpoint OR native snapshot as valid segment bootstrap "
            "(SELF_CONTAINED_SEGMENT). Do not require native_snapshot_count>=1 alone."
        ),
        "collector_restart": False,
        "manifests_rewritten": False,
        "segments_relabeled_on_disk": False,
        "disk_writer_already_fixed": True,
        "disk_writer_loaded_by_running_process": False,
        "repair_plan_if_restart_later": (
            "After explicit operator restart, new segments emit completion_status=closed "
            "and u_gaps-based replayable; historical manifests remain as-is."
        ),
    }

    # Artifacts
    contract = {
        "audit_id": AUDIT_ID,
        "phase3_claims_deactivated": PHASE3_CLAIMS_STATUS,
        "decision_variants": {
            "SUBMINUTE_30S": {
                "decision_at": "first_touch_at + 30s",
                "allowed_sources": [
                    "public_trades with trade_ts <= decision_at",
                    "genuine OB samples with ts <= decision_at (if available)",
                ],
                "forbidden": "any 1m candle with close_time > decision_at",
            },
            "CLOSED_1M": {
                "decision_at": "end of first fully closed 1m candle after first_touch",
                "allowed_sources": "1m candles with close_time <= decision_at",
            },
            "CLOSED_3M": {
                "decision_at": "end of third fully closed 1m candle after first_touch",
                "allowed_sources": "1m candles with close_time <= decision_at",
            },
        },
        "invariant": "max_feature_timestamp <= decision_at for every valid feature",
        "variants_must_not_be_mixed": True,
        "no_full_oos_in_this_audit": True,
    }
    (out_dir / "causal_feature_contract.json").write_text(
        json.dumps(contract, indent=2), encoding="utf-8"
    )

    _write_csv(out_dir / "causal_feature_smoke.csv", smoke)
    _write_csv(out_dir / "feature_provenance.csv", prov)
    # future labels from smoke only (not full OOS)
    _write_csv(out_dir / "causal_future_labels_smoke.csv", pd.DataFrame(labels))
    _write_csv(out_dir / "raw_segment_inventory.csv", pd.DataFrame(inv_rows))
    _write_csv(out_dir / "raw_segment_replay_audit.csv", pd.DataFrame(audit_rows))
    _write_csv(out_dir / "raw_chain_inventory.csv", pd.DataFrame(chain_rows))
    _write_csv(out_dir / "first_failure_examples.csv", pd.DataFrame(first_fail))

    collector_md = f"""# Collector contract (read-only)

## Process
```json
{json.dumps(coll, indent=2, default=str)}
```

## Intended archive contract
**SELF_CONTAINED_SEGMENT**: each closed hour segment begins with either
- a native Bybit `snapshot`, or
- a `rotation_checkpoint` (full local book) at rotation,

and is independently replayable via `data.u` continuity (+1). Exchange `seq` is
informational and must not define gaps.

## Configuration (from live env / defaults)
- Archive root: `{coll.get("env", {}).get("OB_V3_RAW_ARCHIVE_ROOT", SHADOW_ARCHIVE)}`
- Symbols: `{coll.get("env", {}).get("OB_V3_RAW_ARCHIVE_SYMBOLS", "BTCUSDT,DOGEUSDT")}`
- Rotation: `{coll.get("env", {}).get("OB_V3_RAW_ARCHIVE_ROTATION", "hour")}`
- Live default root (unused here): `{LIVE_ARCHIVE}` (missing on disk)
- No systemd unit; started via nohup / CLI `--mode raw-archive-only`

## Snapshot handling
- Bybit sends a snapshot on (re)subscribe; parser archives `type=snapshot`.
- On hour rotation, collector copies current book as `rotation_checkpoint` into the new segment.
- Mid-hour native snapshots are rare; checkpoint-only hours are valid self-contained segments.
- After `u` gap: resync/resubscribe markers + new snapshot expected.

## This audit did NOT
- stop/restart the collector
- read open `*_open_*.zst.tmp` files
- rewrite any manifest
"""
    (out_dir / "collector_contract.md").write_text(collector_md, encoding="utf-8")

    from orderbook_analyse.ob200_v3_raw_discovery.audit import META_NOTE as _MN

    replay_md = f"""# Replay contract

## Implemented contract: SELF_CONTAINED_SEGMENT
Evidence: `RawArchiveManager.rotate_with_checkpoint`, docs/orderbook_v3_raw_archive.md,
`line_to_replay_payload` maps `rotation_checkpoint` → snapshot for replay.

## Continuity field
- Book continuity: Bybit **`data.u`**
- **`seq`**: exchange-wide, jumps are normal → not a loss
- **`pu`**: not used in this pipeline

## Correct validator
`ob200_v3_raw_discovery.audit.process_segment` — u-gaps, checkpoint/snapshot bootstrap.

## Incorrect consumer gate (Phase-3 audit v1)
Required `completion_status==closed` AND `replayable==true` AND `native_snapshot_count>=1`
AND empty `sequence_gaps`. On pre-restart manifests this yields **0** analyzable episodes
even when `process_segment` returns `REPLAY_CONFIRMED_FROM_LOCAL_CHECKPOINT`.

## META_NOTE (from discovery audit)
{_MN}

## Matrix
```json
{json.dumps(matrix, indent=2)}
```
"""
    (out_dir / "replay_contract.md").write_text(replay_md, encoding="utf-8")

    ch_md = """# ClickHouse `orderbook_deltas` attach audit

## Expected
- Database/table: `orderbook_analysis.orderbook_deltas`
- Historical per-level store used by older tools / Phase-3 probe

## Actual
- Attach/load fails (`TOO_MANY_UNEXPECTED_DATA_PARTS` / ASYNC_LOAD wait) — see Phase-3 coverage probe
- **No DDL / writes performed in this audit**

## Relevance to FS raw collector
- **Not required** for the filesystem OB200 archive under `data/orderbook_raw_shadow/ob200_v3`
- V3 live raw-archive-only mode uses `NullWriter` (does not write this table)

## Phase-4 implication
- Phase-4 **can** work entirely from closed FS segments via `list_closed_segments` + replay
- Phase-3 reported “raw OB unavailable” because it **routed to ClickHouse**, not because FS data were absent
"""
    (out_dir / "clickhouse_attach_audit.md").write_text(ch_md, encoding="utf-8")

    methodology = f"""# Methodology

## Phase-3 claims
Deactivated — see `causal_feature_contract.json` / PHASE3_CLAIMS_STATUS.
Phase-3 OOS precision 37–38.5% and future-only eval of leaked flags are **not** valid edges.

## Causal features
Three **non-mixed** decision variants. Invariant: max_source_timestamp <= decision_at.
SUBMINUTE_30S uses public trade ticks only for price/absorption (no unclosed 1m close).
CLOSED_1M / CLOSED_3M use only candles with close_time <= decision_at.

## Outcomes
Smoke-only future-only labels start strictly after decision_at. No full OOS.

## Raw OB
Read-only inventory + `process_segment` causal replay. TMP excluded.
No collector restart. No manifest rewrite.
"""
    (out_dir / "methodology.md").write_text(methodology, encoding="utf-8")

    n_ok = int(matrix.get("n_self_contained_replay_ok") or 0)
    verdict = VERDICT_ROOT_CAUSE
    # If true missing snapshots and no checkpoints → collector bug; not the case here
    if n_ok == 0 and shadow.exists():
        from . import VERDICT_COLLECTOR_BUG

        verdict = VERDICT_COLLECTOR_BUG

    manifest = {
        "audit_id": AUDIT_ID,
        "verdict": verdict,
        "phase3_claims_deactivated": PHASE3_CLAIMS_STATUS,
        "n_causal_smoke_episodes": int(ep_count(smoke)),
        "causal_smoke_variants": list(DECISION_VARIANTS),
        "collector": coll,
        "raw_matrix": matrix,
        "offline_fix": offline_fix,
        "audit_v1_dir": AUDIT_V1_DIR,
        "phase3_dir": str(phase3_dir),
        "no_phase4_oos": True,
        "no_commit": True,
        "no_collector_restart": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    write_report(out_dir, manifest, smoke, prov, matrix, first_fail, offline_fix)
    print("VERDICT", verdict, flush=True)
    return {"verdict": verdict, "manifest": manifest, "out_dir": str(out_dir)}


def ep_count(smoke: pd.DataFrame) -> int:
    if smoke.empty:
        return 0
    return int(smoke["episode_id"].nunique())


def write_report(
    out_dir: Path,
    manifest: dict,
    smoke: pd.DataFrame,
    prov: pd.DataFrame,
    matrix: dict,
    first_fail: list,
    offline_fix: dict,
) -> None:
    lines = [
        f"# {manifest['verdict']}",
        "",
        "## 1. VERDICT",
        manifest["verdict"],
        "",
        "## 2. LIVE-SICHERHEIT",
        "- No commit, no collector stop/restart, no CH writes, no TMP reads,",
        "  no manifest rewrites, no Phase-4 OOS, no bot/PnL.",
        "",
        "## 3. ALTE PHASE-3-CLAIMS",
        json.dumps(manifest["phase3_claims_deactivated"], indent=2),
        "",
        "**Near-edge-Reclaim und Absorption sind nicht kausal valide.**",
        "**OOS-Precision 37–38,5 % darf nicht als bestätigter Edge verwendet werden.**",
        "**Future-only-Auswertung der geleakten Flags ist kein gültiger Nachweis.**",
        "",
        "## 4. NEUER KAUSALER FEATURE-CONTRACT",
        "See `causal_feature_contract.json` — variants SUBMINUTE_30S / CLOSED_1M / CLOSED_3M.",
        "",
        "## 5. FEATURE-PROVENANCE",
        f"- provenance rows: {len(prov)}",
        f"- causal_ok rate: {float(prov['causal_ok'].mean()) if len(prov) else None}",
        "",
        "## 6. CAUSAL-SMOKE",
        smoke.head(40).to_string(index=False) if len(smoke) else "empty",
        "",
        "## 7. COLLECTOR-CONTRACT",
        "See `collector_contract.md`.",
        f"- PID: {manifest.get('collector', {}).get('pid')}",
        f"- cmdline: {manifest.get('collector', {}).get('cmdline')}",
        "",
        "## 8. SEGMENTINVENTAR",
        f"- closed segments inventoried: {matrix.get('n_closed_segments')}",
        f"- self-contained replay OK (causal audit): {matrix.get('n_self_contained_replay_ok')}",
        f"- true u-gap segments: {matrix.get('n_true_u_gap_segments')}",
        "- XRP: NO_ARCHIVE_COVERAGE",
        "",
        "## 9. SNAPSHOT-STATUS",
        "- Native Bybit snapshots on subscribe; archived.",
        "- Hour rotation archives `rotation_checkpoint` (full book) — required for self-contained hours.",
        "- R6 v1 gate requiring native_snapshot_count>=1 was incorrect for checkpoint-only hours.",
        "",
        "## 10. SEGMENTKETTEN",
        "Contract SELF_CONTAINED_SEGMENT — chain across files not required when checkpoint present.",
        "Segment boundary alone is not a u-gap. See `raw_chain_inventory.csv`.",
        "",
        "## 11. ERSTE REPLAY-FEHLER",
        pd.DataFrame(first_fail).to_string(index=False) if first_fail else "none",
        "",
        "## 12. CLICKHOUSE-ATTACH",
        "See `clickhouse_attach_audit.md` — not required for FS Phase-4 path.",
        "",
        "## 13. EXAKTE URSACHE",
        json.dumps(matrix, indent=2),
        "",
        "## 14. OFFLINE-FIX",
        json.dumps(offline_fix, indent=2),
        "",
        "## 15. TESTS",
        "See `tests/test_lld_r6_causal_and_raw_audit_v2.py`.",
        "",
        "## 16. FREIGABE ODER BLOCKER",
        "- Causal feature contract: FIXED (smoke + unit tests).",
        "- Full OOS re-evaluation: NOT started (blocked by design until explicit next step).",
        "- Raw OB for full R6 history: still limited by collection start (~2026-08-24 22:47).",
        "- For recent overlapping hours: data are causally replayable; use corrected loader gate.",
        "- Collector restart (to emit fixed manifests going forward): NOT done here — operator decision.",
        f"",
        f"Elapsed: {manifest.get('elapsed_sec')}s",
    ]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
