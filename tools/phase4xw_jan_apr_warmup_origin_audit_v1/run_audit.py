#!/usr/bin/env python3
"""Phase 4X-W read-only Jan–Apr warmup & ACTIVE origin completeness audit."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from calendar import monthrange
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4xw_jan_apr_warmup_origin_audit_v1"
MULTI = (
    ROOT
    / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"
)
PROD = MULTI / "production/results"
STAG = MULTI / "staging/phase6xt_5m_pd1_jan_sep_v1"
MANIFEST = STAG / "run/manifest.json"
ADAPTER = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_density_trp_adapter.js"
)
ANALYZER = ROOT / "tools/phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js"

FORENSIC_CLOSES = [
    "2026-04-11T00:00:00Z",
    "2026-04-15T12:00:00Z",
    "2026-04-30T23:55:00Z",
    "2026-05-01T00:00:00Z",
    "2026-05-01T12:00:00Z",
    "2026-05-07T00:00:00Z",
    "2026-05-15T12:00:00Z",
    "2026-05-31T23:55:00Z",
]

CONTINUITY_PAIRS = [
    ("2026-04-30T23:55:00Z", "2026-05-01T00:00:00Z"),
    ("2026-05-01T00:00:00Z", "2026-05-07T00:00:00Z"),
    ("2026-05-07T00:00:00Z", "2026-05-15T12:00:00Z"),
    ("2026-05-15T12:00:00Z", "2026-05-31T23:55:00Z"),
]


def _parse_utc(s: str) -> datetime:
    s = s.strip().replace(" ", "T")
    if not s.endswith("Z") and "+" not in s:
        s += "Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _stem(dc: datetime) -> str:
    return dc.strftime("%Y%m%dT%H%M%SZ")


def resolve_json_path(close_iso: str) -> Path | None:
    dc = _parse_utc(close_iso)
    stem = _stem(dc)
    y, m = dc.year, dc.month
    for base in (PROD / f"2026-{m:02d}/5m_PD1", STAG / "results" / f"2026-{m:02d}/5m_PD1"):
        p = base / f"{stem}.json"
        if p.is_file():
            return p
    return None


def origin_unix(pool_id: str) -> int | None:
    try:
        return int(pool_id.rsplit(":", 1)[-1])
    except ValueError:
        return None


def origin_logical(pool_id: str) -> str:
    parts = pool_id.split(":")
    if len(parts) >= 6 and parts[0] == "lldv2":
        return f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[4]}:{parts[5]}"
    return pool_id


def load_snapshot(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def active_origins(payload: dict) -> list[dict[str, Any]]:
    ids = payload.get("active_pool_ids") or []
    pools = payload.get("pools") or {}
    rows = []
    for pid in ids:
        p = pools.get(pid) or {}
        ou = origin_unix(pid)
        odt = datetime.fromtimestamp(ou, tz=timezone.utc) if ou else None
        rows.append(
            {
                "pool_id": pid,
                "origin_logical": origin_logical(pid),
                "origin_unix": ou,
                "origin_utc": odt.isoformat().replace("+00:00", "Z") if odt else "",
                "side": p.get("side"),
                "bottom": p.get("bottom_price"),
                "top": p.get("top_price"),
                "known_at": p.get("known_at"),
            }
        )
    return rows


def month_bucket(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def analyze_close(close_iso: str) -> dict[str, Any]:
    path = resolve_json_path(close_iso)
    if not path:
        return {"decision_close_utc": close_iso, "status": "NOT_AVAILABLE", "path": ""}
    payload = load_snapshot(path)
    dc = _parse_utc(payload.get("decision_close") or close_iso)
    rows = active_origins(payload)
    hfc = _parse_utc(payload["history_from_close"]) if payload.get("history_from_close") else None
    cls = _parse_utc(payload["candle_load_start"]) if payload.get("candle_load_start") else None
    by_month = Counter()
    older_168h = 0
    oldest: datetime | None = None
    for r in rows:
        if r["origin_utc"]:
            odt = _parse_utc(r["origin_utc"])
            by_month[month_bucket(odt)] += 1
            if oldest is None or odt < oldest:
                oldest = odt
            if (dc - odt).total_seconds() > 168 * 3600:
                older_168h += 1
    fi = payload.get("frozen_input") or {}
    return {
        "decision_close_utc": close_iso,
        "status": "OK",
        "path": str(path.relative_to(ROOT)),
        "source_tree": "production" if "/production/" in str(path) else "staging",
        "engine_input_hash": payload.get("engine_input_hash"),
        "active_state_fingerprint": payload.get("active_state_fingerprint"),
        "frozen_snapshot_id": fi.get("snapshot_id"),
        "frozen_price_hash": fi.get("price_hash"),
        "candle_load_start": payload.get("candle_load_start"),
        "history_from_close": payload.get("history_from_close"),
        "candle_count": payload.get("candle_count"),
        "prefix_candle_fingerprint": payload.get("prefix_candle_fingerprint"),
        "active_pool_count": len(rows),
        "active_upper": sum(1 for r in rows if r["side"] == "upper"),
        "active_lower": sum(1 for r in rows if r["side"] == "lower"),
        "origins_jan": by_month.get("2026-01", 0),
        "origins_feb": by_month.get("2026-02", 0),
        "origins_mar": by_month.get("2026-03", 0),
        "origins_apr": by_month.get("2026-04", 0),
        "origins_may": by_month.get("2026-05", 0),
        "origins_older_than_168h": older_168h,
        "oldest_origin_utc": oldest.isoformat().replace("+00:00", "Z") if oldest else "",
        "origin_age_hours_at_close": round((dc - oldest).total_seconds() / 3600, 2) if oldest else "",
    }


def pools_for_density(payload: dict, close_iso: str) -> list[dict]:
    """Single-instant ACTIVE pools as landscape segments for adapter."""
    iso = close_iso.replace("Z", "").replace("+00:00", "") + "Z"
    ids = payload.get("active_pool_ids") or []
    pools_map = payload.get("pools") or {}
    out = []
    for pid in ids:
        p = pools_map.get(pid) or {}
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


def density_at_close(close_iso: str, y_min: float, y_max: float) -> dict[str, Any]:
    path = resolve_json_path(close_iso)
    if not path:
        return {"error": "NOT_AVAILABLE"}
    payload = load_snapshot(path)
    pools = pools_for_density(payload, close_iso)
    t_ms = int(_parse_utc(close_iso).timestamp() * 1000)
    script = f"""
const fs = require('fs');
const vm = require('vm');
const ctx = {{ window: {{}}, console, Date, performance: {{ now: () => 0 }} }};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync({json.dumps(str(ADAPTER))}, 'utf8'), ctx);
const AD = ctx.window.PD1ExactDensityTrpAdapter;
const pools = {json.dumps(pools)};
const t = {t_ms};
const yMin = {y_min};
const yMax = {y_max};
function bands(side) {{
  const raw = AD.computeExactBandsForSide(pools, t, side, yMin, yMax);
  const zones = AD.poolsToExactOriginZones(pools, t, t, yMin, yMax, {{ sweepT0Ms: t, sweepT1Ms: t, slotMinutes: 5 }});
  const culled = AD.cullZonesToViewport ? AD.cullZonesToViewport(zones, {{ fromMs: t, toMs: t, yMin, yMax }}) : zones;
  const merged = AD.mergeExactOriginSegments(culled);
  const d2 = AD.filterSegmentsByMinOrigins(merged, 2);
  return {{ rawCount: raw.length, mergedCount: merged.length, depth2Count: d2.length, depth2: d2 }};
}}
console.log(JSON.stringify({{ lower: bands('lower'), upper: bands('upper') }}));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return {"error": proc.stderr or proc.stdout}
    return json.loads(proc.stdout)


def ch_monthly_1m() -> list[dict]:
    sys.path.insert(0, str(ROOT / "backtester/short/dashboard"))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    rows = []
    q = f"""
    SELECT toStartOfMonth(open_time) AS mo, count() AS n, min(open_time) AS mn, max(open_time) AS mx
    FROM {cfg.database}.{cfg.table} FINAL
    WHERE symbol='DOGEUSDT' AND interval='1m' AND is_closed=1
      AND open_time >= '2026-01-01' AND open_time < '2026-05-01'
    GROUP BY mo ORDER BY mo
    """
    for r in client.query(q).named_results():
        y, m = int(str(r["mo"])[:4]), int(str(r["mo"])[5:7])
        exp = monthrange(y, m)[1] * 24 * 60
        rows.append(
            {
                "month": f"{y:04d}-{m:02d}",
                "earliest_1m_utc": str(r["mn"]),
                "latest_1m_utc": str(r["mx"]),
                "count_1m": r["n"],
                "expected_1m": exp,
                "gaps_vs_expected": max(0, exp - int(r["n"])),
                "lmv2_5m_pd1_snapshots_in_month": 0,
                "provenance_status": "AVAILABLE_INPUT_OHLCV_ONLY",
            }
        )
    client.close()
    # snapshot counts per month on disk
    for y, m in [(2026, 1), (2026, 2), (2026, 3), (2026, 4)]:
        key = f"{y:04d}-{m:02d}"
        n = 0
        for base in (PROD, STAG / "results"):
            d = base / f"2026-{m:02d}/5m_PD1"
            if d.is_dir():
                n = max(n, len(list(d.glob("*.json"))))
        for row in rows:
            if row["month"] == key:
                row["lmv2_5m_pd1_snapshots_in_month"] = n
                if m == 4 and n:
                    row["provenance_status"] = "PARTIAL_FROZEN_APR_HEAD_PLUS_PRODUCTION_TAIL"
                elif n == 0:
                    row["provenance_status"] = "NO_5M_PD1_FROZEN_NOT_VERIFIED_POLICY"
    return rows


def continuity_row(a_iso: str, b_iso: str) -> dict:
    pa, pb = resolve_json_path(a_iso), resolve_json_path(b_iso)
    if not pa or not pb:
        return {"from_close": a_iso, "to_close": b_iso, "status": "NOT_AVAILABLE"}
    sa = {r["pool_id"] for r in active_origins(load_snapshot(pa))}
    sb = {r["pool_id"] for r in active_origins(load_snapshot(pb))}
    lost = sa - sb
    gained = sb - sa
    return {
        "from_close": a_iso,
        "to_close": b_iso,
        "active_from": len(sa),
        "active_to": len(sb),
        "persisted": len(sa & sb),
        "lost_ids": len(lost),
        "gained_ids": len(gained),
        "lost_sample": "|".join(sorted(lost)[:5]),
        "gained_sample": "|".join(sorted(gained)[:5]),
        "engine_version_change": "NOT_TRACKED_IN_JSON",
    }


def wcsv(name: str, rows: list[dict]) -> None:
    p = OUT / name
    if not rows:
        p.write_text("", encoding="utf-8")
        return
    fields = sorted({k for r in rows for k in r})
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else {}

    engine_rows = [
        {
            "artifact": "phase6xt_5m_pd1_jan_sep_v1/run/manifest.json",
            "warmup_policy": manifest.get("warmup_policy"),
            "excluded_not_verified": manifest.get("allowed_scope", {}).get("excluded_not_verified"),
            "april_frozen_id": manifest.get("allowed_scope", {}).get("april_head_utc", {}).get(
                "frozen_snapshot_id"
            ),
            "september_frozen_id": manifest.get("allowed_scope", {}).get("september_utc", {}).get(
                "frozen_snapshot_id"
            ),
            "may_aug_regen_skip": ",".join(manifest.get("allowed_scope", {}).get("regen_skip_complete_months", [])),
            "provenance_class": "DECLARED_INPUT",
            "notes": "May production files not rewritten by overnight run per manifest",
        }
    ]
    # sample May provenance from file
    may_path = resolve_json_path("2026-05-01T12:00:00Z")
    if may_path:
        pl = load_snapshot(may_path)
        fi = pl.get("frozen_input") or {}
        engine_rows.append(
            {
                "artifact": str(may_path.relative_to(ROOT)),
                "warmup_policy": "rolling_history_from_close(4 cal months) per JSON field",
                "excluded_not_verified": "",
                "april_frozen_id": "",
                "september_frozen_id": fi.get("snapshot_id"),
                "may_aug_regen_skip": "pre-existing production",
                "provenance_class": "HASH_VERIFIED_INPUT_IN_SNAPSHOT",
                "notes": f"candle_load_start={pl.get('candle_load_start')} history_from={pl.get('history_from_close')}",
            }
        )
    wcsv("engine_input_provenance.csv", engine_rows)

    wcsv("jan_apr_input_coverage.csv", ch_monthly_1m())

    inv = [analyze_close(c) for c in FORENSIC_CLOSES]
    wcsv("active_origin_inventory.csv", inv)

    legacy = []
    for row in inv:
        if row.get("status") != "OK":
            continue
        if row.get("origins_jan") or row.get("origins_feb") or row.get("origins_mar"):
            legacy.append(
                {
                    "decision_close_utc": row["decision_close_utc"],
                    "origins_before_april": (
                        row.get("origins_jan", 0)
                        + row.get("origins_feb", 0)
                        + row.get("origins_mar", 0)
                    ),
                    "origins_older_than_168h": row.get("origins_older_than_168h"),
                    "oldest_origin_utc": row.get("oldest_origin_utc"),
                    "interpretation": "PERSISTENT_ACTIVE_IN_FROZEN_SNAPSHOT_NOT_INDEPENDENT_COMPLETENESS_PROOF",
                }
            )
    wcsv("legacy_active_origins.csv", legacy)

    wcsv("origin_continuity.csv", [continuity_row(a, b) for a, b in CONTINUITY_PAIRS])

    # May reference density
    ref = "2026-05-01T12:00:00Z"
    path = resolve_json_path(ref)
    bands_out: list[dict] = []
    membership_out: list[dict] = []
    if path:
        payload = load_snapshot(path)
        prices = [float((payload.get("pools") or {}).get(pid, {}).get("bottom_price", 0)) for pid in payload.get("active_pool_ids", [])]
        prices += [float((payload.get("pools") or {}).get(pid, {}).get("top_price", 0)) for pid in payload.get("active_pool_ids", [])]
        lo, hi = min(prices) * 0.995, max(prices) * 1.005
        dens = density_at_close(ref, lo, hi)
        for side in ("lower", "upper"):
            for i, z in enumerate(dens.get(side, {}).get("depth2", [])):
                bands_out.append(
                    {
                        "decision_close_utc": ref,
                        "audit_mode": "OFFLINE_FROZEN_DENSITY_AUDIT",
                        "side": side,
                        "band_index": i,
                        "lower_bound": z.get("bottom_price") or z.get("metadata", {}).get("bottom"),
                        "upper_bound": z.get("top_price") or z.get("metadata", {}).get("top"),
                        "unique_origin_depth": (z.get("metadata") or {}).get("depth"),
                        "chart_parity_may": "NOT_CLAIMED_CH_NOT_PUBLISHED",
                    }
                )
        # Forensic band membership via analyzer helper
        if ANALYZER.is_file():
            pools = pools_for_density(payload, ref)
            proc = subprocess.run(
                [
                    "node",
                    "-e",
                    f"""
const {{ loadAdapter, computeExactBandsForSideWithOrigins }} = require({json.dumps(str(ANALYZER))});
const {{ AD }} = loadAdapter({json.dumps(str(ADAPTER))});
const pools = {json.dumps(pools)};
const t = {int(_parse_utc(ref).timestamp()*1000)};
const out = [];
for (const side of ['lower','upper']) {{
  for (const b of computeExactBandsForSideWithOrigins(pools, t, side, {lo}, {hi})) {{
    if (b.depth >= 2) out.push({{ side, bottom: b.y0, top: b.y1, depth: b.depth, origins: (b.origins||[]).join('|') }});
  }}
}}
console.log(JSON.stringify(out));
""",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode == 0:
                for i, b in enumerate(json.loads(proc.stdout)):
                    membership_out.append(
                        {
                            "decision_close_utc": ref,
                            "side": b["side"],
                            "band_index": i,
                            "lower_bound": b["bottom"],
                            "upper_bound": b["top"],
                            "unique_origin_count": b["depth"],
                            "origin_ids": b["origins"],
                            "oldest_origin_in_band": "",
                        }
                    )
    wcsv("may01_density_bands.csv", bands_out)
    wcsv("density_membership_details.csv", membership_out)

    gaps = [
        {
            "gap_id": "NO_5M_SNAPSHOTS_JAN_MAR",
            "status": "NOT_VERIFIED",
            "impact": "No frozen 5m PD1 files; cannot cross-check origin history before April head",
        },
        {
            "gap_id": "APR_1_TO_10_EXCLUDED",
            "status": "DECLARED_IN_MANIFEST",
            "impact": "28800 closes policy excluded; gap before 2026-04-11 first snapshot",
        },
        {
            "gap_id": "MAY_PRODUCTION_REGEN_SKIP",
            "status": "NOT_VERIFIED_EXECUTION",
            "impact": "May files pre-date phase6xt overnight; no manifest execution log for May regen in this audit",
        },
        {
            "gap_id": "ACTIVE_ORIGIN_COMPLETENESS",
            "status": "ACTIVE_ORIGIN_COMPLETENESS_NOT_VERIFIED",
            "impact": "No independent full origin replay; only snapshot-internal evidence",
        },
        {
            "gap_id": "CHART_MAY_PARITY",
            "status": "NOT_AVAILABLE",
            "impact": "PublishedActiveReader has no May 5m PD1 publishes",
        },
    ]
    wcsv("provenance_gaps.csv", gaps)

    unexplained = []
    for c in CONTINUITY_PAIRS:
        r = continuity_row(c[0], c[1])
        if r.get("status") == "NOT_AVAILABLE":
            continue
        if r["lost_ids"] > 0:
            unexplained.append(
                {
                    "from_close": r["from_close"],
                    "to_close": r["to_close"],
                    "lost_active_count": r["lost_ids"],
                    "classification": "REQUIRES_LIFECYCLE_REVIEW_NOT_AUTO_DEACTIVATED",
                    "sample_lost": r["lost_sample"],
                }
            )
    wcsv("unexplained_origin_losses.csv", unexplained)

    (OUT / "warmup_requirements.md").write_text(
        """# Warmup requirements (read-only synthesis)

## Research chart context (168h)
- `historical_window_v1.WARMUP_HOURS = 168` — **display/export** context only; does not truncate ACTIVE origins in frozen JSON.

## Original LMV2 frozen snapshot (5m PD1 May example `20260501T120000Z`)
- `history_from_close`: four **calendar months** before decision close (`2026-01-01T12:00:00Z` for May 1 12:00).
- `candle_load_start`: preset warmup before `history_from_close` (example: `2025-12-20T00:30:00Z`).
- `candle_count`: embedded prefix size in snapshot metadata (example: 38154).

## Distinction
| Layer | May 2026-05-01T12:00Z evidence |
|-------|--------------------------------|
| 168h research rule | Satisfied by much older `candle_load_start` |
| Engine candle prefix | From **2025-12-20** (DECLARED in JSON) |
| Visible history window | From **2026-01-01** (`history_from_close`) |
| Jan–Mar in CH 1m | AVAILABLE_INPUT |
| Jan–Mar PROVEN in May regen run | **NOT_VERIFIED** (regen_skip) |

## Four months rule
Does **not** prove all origins since market inception — only back to `history_from_close`. Origins older than that must not appear; audit checks oldest origin vs `history_from_close` per close in inventory CSV.
""",
        encoding="utf-8",
    )

    summary = {
        "forensic_closes": inv,
        "may01_band_count": len(bands_out),
        "verdict_candidates": "see report",
    }
    (OUT / "audit_summary.md").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
