#!/usr/bin/env python3
"""Phase 4X-AD — May 5m PD1 catalog/provenance bridge (read-only on production catalogs)."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4xad_may_catalog_bridge_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE4XAD_MAY_CATALOG_PROVENANCE_BRIDGE_V1.md"
BRIDGE = OUT / "bridge_catalog_doge_apr_sep_v1"

SPREAD_DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
POOLS_DASH = ROOT / "backtester" / "short" / "dashboard"
TRP = Path("/home/telgenbuescher/projects/trading_research_platform")

APR_SEP = POOLS_DASH / "research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
MULTI_MAY = (
    POOLS_DASH
    / "research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"
    / "production/results/2026-05/5m_PD1"
)
PARITY_MANIFEST = ROOT / "results/phase4xab_full_may_density_continuity_v1/may_close_parity_manifest.json"

PILOT_START = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
PILOT_N = 20
PIPELINE_LINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
MONTH = "2026-05"
COMBO = "5m_PD1"


def _setup_path() -> None:
    dash = SPREAD_DASH if SPREAD_DASH.is_dir() else POOLS_DASH
    for mod in list(sys.modules):
        if mod.startswith("research_charts"):
            del sys.modules[mod]
    sys.path = [p for p in sys.path if p != str(POOLS_DASH)]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if TRP.is_dir() and str(TRP) not in sys.path:
        sys.path.insert(0, str(TRP))
    if str(dash) not in sys.path:
        sys.path.insert(0, str(dash))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _pilot_stems() -> list[str]:
    return [
        (PILOT_START + timedelta(minutes=5 * i)).strftime("%Y%m%dT%H%M%SZ") for i in range(PILOT_N)
    ]


def _file_hashes(paths: list[Path]) -> dict[str, str]:
    return {str(p): _sha256_file(p) for p in paths}


def build_bridge_catalog(stems: list[str]) -> dict[str, Any]:
    """Isolated bridge tree under results/ — does not touch APR_SEP production files."""
    prod = BRIDGE / "production"
    may_dest = prod / "results" / MONTH / COMBO
    may_dest.mkdir(parents=True, exist_ok=True)

    # Immutable copies of apr_sep governance files (byte-identical at copy time).
    for name in ("catalog_manifest.json", "coverage_matrix.json"):
        src = APR_SEP / "production" / name
        dst = prod / name
        shutil.copy2(src, dst)

    lookup_src = APR_SEP / "production" / "provenance_lookup.json"
    lookup_data = json.loads(lookup_src.read_text(encoding="utf-8"))
    lookup_before_sha = _sha256_file(lookup_src)

    staging_rows: list[dict[str, str]] = []
    rel_keys: list[str] = []
    for stem in stems:
        src = MULTI_MAY / f"{stem}.json"
        dst = may_dest / f"{stem}.json"
        shutil.copy2(src, dst)
        src_sha = _sha256_file(src)
        dst_sha = _sha256_file(dst)
        if src_sha != dst_sha:
            raise RuntimeError(f"byte mismatch after copy {stem}")
        rel = f"production/results/{MONTH}/{COMBO}/{stem}.json"
        rel_keys.append(rel)
        payload = json.loads(src.read_text(encoding="utf-8"))
        fid = str((payload.get("frozen_input") or {}).get("snapshot_id") or "")
        lookup_data.setdefault("entries", {})[rel] = {
            "source_frozen_id": fid,
            "dest_sha256": dst_sha,
        }
        staging_rows.append(
            {
                "stem": stem,
                "original_path": str(src),
                "staging_path": str(dst),
                "relative_dest": rel,
                "original_sha256": src_sha,
                "staging_sha256": dst_sha,
                "byte_identical": "YES",
                "frozen_input_snapshot_id": fid,
            }
        )

    lookup_data["entry_count"] = len(lookup_data.get("entries") or {})
    lookup_data["phase4xad_bridge"] = {
        "kind": "ADDED_VERIFICATION_METADATA",
        "added_may_5m_pd1_stems": stems,
        "added_entry_count": len(stems),
        "base_lookup_sha256": lookup_before_sha,
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    lookup_dst = prod / "provenance_lookup.json"
    lookup_dst.write_text(json.dumps(lookup_data, indent=2, sort_keys=True), encoding="utf-8")

    bridge_note = {
        "catalog_root": str(BRIDGE),
        "original_provenance": "doge_apr_sep_2026_v1_history production manifests (copied, not mutated)",
        "added_verification": "May 2026 5m_PD1 pilot stems staged under bridge production/results only",
        "production_apr_sep_touched": False,
    }
    (prod / "phase4xad_bridge_note.json").write_text(json.dumps(bridge_note, indent=2), encoding="utf-8")

    return {
        "staging_rows": staging_rows,
        "rel_keys": rel_keys,
        "lookup_before_sha": lookup_before_sha,
        "lookup_bridge_sha": _sha256_file(lookup_dst),
        "apr_sep_lookup_unchanged_sha": lookup_before_sha,
    }


def run_validate_batch(stems: list[str]) -> dict[str, Any]:
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
        BatchSpec,
        validate_batch,
    )
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.historical_batch_config import (
        StrategyCombo,
    )

    combo = StrategyCombo("5m", 1)
    spec = BatchSpec(
        month=MONTH,
        combo_folder=COMBO,
        stems=tuple(stems),
        pipeline_line_id=PIPELINE_LINE,
        import_batch_id="phase4xad-pilot-20-readonly",
    )
    return validate_batch(BRIDGE, spec, test_run_id="00000000-0000-4000-8000-000000004dad")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    _setup_path()
    stems = _pilot_stems()

    apr_paths = [
        APR_SEP / "production/catalog_manifest.json",
        APR_SEP / "production/coverage_matrix.json",
        APR_SEP / "production/provenance_lookup.json",
    ]
    apr_hashes_before = _file_hashes(apr_paths)

    bridge_info = build_bridge_catalog(stems)
    apr_hashes_after = _file_hashes(apr_paths)
    if apr_hashes_before != apr_hashes_after:
        raise RuntimeError("apr_sep production catalog files changed — STOP")

    with (OUT / "may_source_to_staging_manifest.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(bridge_info["staging_rows"][0].keys()))
        w.writeheader()
        w.writerows(bridge_info["staging_rows"])

    with (OUT / "original_vs_staging_hashes.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["stem", "original_sha256", "staging_sha256", "match"])
        w.writeheader()
        for row in bridge_info["staging_rows"]:
            w.writerow(
                {
                    "stem": row["stem"],
                    "original_sha256": row["original_sha256"],
                    "staging_sha256": row["staging_sha256"],
                    "match": row["byte_identical"],
                }
            )

    vb = run_validate_batch(stems)
    (OUT / "validate_bundle_result.json").write_text(json.dumps(vb, indent=2, default=str), encoding="utf-8")

    parity_roll = json.loads(PARITY_MANIFEST.read_text(encoding="utf-8")) if PARITY_MANIFEST.is_file() else {}
    provenance_bridge = {
        "ORIGINAL_PROVENANCE": {
            "catalog_id": "doge_apr_sep_2026_v1",
            "catalog_root_production": str(APR_SEP),
            "multi_month_frozen_source": str(MULTI_MAY),
            "immutable_input_snapshot_id": "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b",
            "engine": "live_micro_lld_v2@trp",
            "phase4xab_verdict": "MAY_DENSITY_CONTINUITY_VERIFIED",
            "phase4xab_manifest_sha256": parity_roll.get("manifest_sha256"),
        },
        "ADDED_VERIFICATION_METADATA": {
            "phase": "4X-AD",
            "bridge_catalog_root": str(BRIDGE),
            "pilot_stems": stems,
            "provenance_entries_added": len(stems),
            "lookup_base_sha256": bridge_info["lookup_before_sha"],
            "lookup_bridge_sha256": bridge_info["lookup_bridge_sha"],
        },
        "publish_target": {
            "pipeline_line_id": PIPELINE_LINE,
            "note": "CH publish not executed in 4X-AD",
        },
    }
    (OUT / "provenance_bridge.json").write_text(json.dumps(provenance_bridge, indent=2), encoding="utf-8")

    n_ok = len(vb.get("closes") or [])
    n_err = len(vb.get("errors") or [])
    all_pass = n_err == 0 and n_ok == PILOT_N

    _write_docs(vb, stems, apr_hashes_before, all_pass)
    _write_report(all_pass, n_ok, n_err, stems)

    if all_pass:
        print("MAY_CATALOG_PILOT_VALIDATED", n_ok)
        return 0
    if n_ok > 0:
        print("MAY_CATALOG_PILOT_PARTIAL", n_ok, n_err)
        return 1
    print("MAY_CATALOG_PILOT_BLOCKED", vb.get("errors", [])[:3])
    return 1


def _write_docs(vb: dict, stems: list[str], apr_hashes: dict[str, str], all_pass: bool) -> None:
    (OUT / "catalog_gate_root_cause.md").write_text(
        """# Catalog gate root cause (Phase 4X-AD)

## Why `validate_bundle` failed in Phase 4X-AC

Two independent fail-closed checks in `historical_json_import/validate.py`:

### 1. Path subpath (`relative_dest_for`)

```50:51:backtester/short/dashboard/research_charts/historical_lmv2/provenance_lookup.py
def relative_dest_for(production_root: Path, snapshot_path: Path) -> str:
    return _relative_dest(production_root, snapshot_path)
```

`validate_provenance` calls `relative_dest_for(catalog_root, bundle["path"])`. The frozen JSON lived under `doge_multi_month_2026_history/...` while `catalog_root` was `doge_apr_sep_2026_v1_history` → **`ValueError: … is not in the subpath of …`**.

### 2. Provenance lookup entry (would fail next)

```34:42:backtester/short/dashboard/research_charts/historical_lmv2/provenance_lookup.py
    def verify_file(self, relative_dest: str, path: Path, payload_frozen_id: str) -> None:
        entry = self.get(relative_dest)
        ...
        if digest != entry.dest_sha256:
            raise Sha256MismatchError(...)
```

`doge_apr_sep` `provenance_lookup.json` contains `production/results/2026-09/5m_PD1/*` (2592 stems) but **no** `production/results/2026-05/5m_PD1/*`.

### 3. Manifest gates (`discover_mixed_frozen_catalog`)

```45:67:backtester/short/dashboard/research_charts/historical_lmv2/mixed_catalog.py
def discover_mixed_frozen_catalog(production_root: Path, *, name: str | None = None) -> MixedFrozenProductionCatalog:
    manifest_path = root / "production" / "catalog_manifest.json"
    ...
    authorized = frozenset(str(x) for x in manifest["authorized_frozen_input_ids"])
```

`cfae2571…` is already authorized. Coverage matrix has no `5m_PD1` / `2026-05` row, but `validate_bundle` does **not** require per-file coverage rows — only manifest + provenance + semantics.

## Bridge resolution (no guard bypass)

- Byte-copy pilot JSON into **isolated** `bridge_catalog_doge_apr_sep_v1/production/results/2026-05/5m_PD1/`.
- Copy apr_sep `catalog_manifest.json` + `coverage_matrix.json` unchanged into bridge.
- Copy apr_sep `provenance_lookup.json` and **append** 20 May entries (documented as ADDED_VERIFICATION_METADATA).
- Run `build_import_bundle` → `validate_bundle` via `validate_batch` with `catalog_root=BRIDGE`.

Production `doge_apr_sep_2026_v1_history` files were not modified.
""",
        encoding="utf-8",
    )

    (OUT / "original_catalog_integrity.md").write_text(
        f"""# Original catalog integrity

Production `doge_apr_sep_2026_v1_history` SHA-256 before and after bridge build (must match):

```json
{json.dumps(apr_hashes, indent=2)}
```

- `catalog_id` remains **`doge_apr_sep_2026_v1`** on the bridge **copy** only.
- No writes to `doge_multi_month` frozen originals.
- September 5m PD1 on-disk paths under apr_sep unchanged.

Bridge note: `bridge_catalog_doge_apr_sep_v1/production/phase4xad_bridge_note.json`
""",
        encoding="utf-8",
    )

    (OUT / "september_non_interference.md").write_text(
        """# September non-interference

- ClickHouse publish line `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` not written in this phase.
- apr_sep production `provenance_lookup.json` unchanged (May entries exist only on bridge copy).
- Sep stems remain at `production/results/2026-09/5m_PD1/` in apr_sep with existing lookup keys.
- Pilot validation uses only May stems under bridge tree; no Sep files re-hashed or moved.
""",
        encoding="utf-8",
    )

    pilot_md = ["# Pilot 20-close validation\n", f"Window: `{stems[0]}` … `{stems[-1]}`\n"]
    for c in vb.get("closes") or []:
        pilot_md.append(f"- **{c['stem']}**: snapshot `{c['snapshot_id'][:16]}…`, active={c['active_pool_count']}, frozen_id=`{c['frozen_input_id'][:16]}…`")
    pilot_md.append(f"\n**validate_batch errors:** {len(vb.get('errors') or [])}\n")
    if vb.get("errors"):
        for e in vb["errors"][:25]:
            pilot_md.append(f"- `{e}`")
    pilot_md.append(f"\n**Overall:** {'PASS' if all_pass else 'FAIL'}\n")
    (OUT / "pilot_20_close_validation.md").write_text("\n".join(pilot_md), encoding="utf-8")

    closes_detail = vb.get("closes") or []
    agg_members = sum(c.get("active_pool_count", 0) for c in closes_detail)
    (OUT / "pilot_publish_execution_plan.md").write_text(
        f"""# Pilot publish execution plan (NOT EXECUTED)

**Precondition:** explicit human approval + `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1`.

## Scope

- 20 closes: `{stems[0]}` … `{stems[-1]}`
- Line: `{PIPELINE_LINE}`
- Catalog root for import: production `doge_apr_sep_2026_v1_history` after COPY_VERIFIED + provenance append (mirror bridge)

## Per-close CH writes (existing `import_historical_close`)

| Table | Rows per close | Pilot 20 aggregate |
|-------|----------------|-------------------|
| `research_lmv2_active_snapshot_meta_v1` | 1 | 20 |
| `research_lmv2_active_snapshot_member_v1` | active_count | {agg_members} |
| `research_lmv2_snapshot_active_pool_v1` | active_count | {agg_members} |
| `research_lmv2_active_snapshot_publish_v1` | 1 | 20 |

## Order (batch path)

1. `write_active_snapshot_staged`
2. `write_active_pool_catalog_from_json_rows`
3. Sidecar `write_and_verify` + `require_before_publish`
4. `commit_publish_ledger`

## Idempotency / conflicts

- `resolve_existing_publish` with `allow_existing_publish=True` skips identical fingerprint.
- `PublishConflictError` on snapshot_id / fingerprint mismatch — **STOP**, no Sep overwrite.

## Readback

- `PublishedActiveReader.load_published` per stem
- Density manifest parity vs Phase 4X-AB frozen hash
- `build_landscape_window` smoke on pilot window

## Recovery

- Do not delete Sep rows; fix-forward only on failed May batch.
- Restore provenance_lookup from backup if partial catalog write.

## Abort

Any gate A–F failure from Phase 4X-AC chart_backtester_parity_gates → no full May expansion.
""",
        encoding="utf-8",
    )

    blockers = []
    if not all_pass:
        blockers.append("validate_bundle errors remain — see validate_bundle_result.json")
    else:
        blockers.append(
            "Bridge pilot PASS does not imply production apr_sep updated — mirror COPY + provenance append still required before CH writes"
        )
    blockers.append(
        "Production apr_sep still lacks May 5m_PD1 files + provenance rows on live catalog root"
    )
    blockers.append("Full May 8928 COPY_VERIFIED + provenance append not in scope of 4X-AD pilot tree")
    (OUT / "remaining_blockers.md").write_text(
        "# Remaining blockers\n\n" + "\n".join(f"- {b}" for b in blockers) + "\n",
        encoding="utf-8",
    )


def _write_report(all_pass: bool, n_ok: int, n_err: int, stems: list[str]) -> None:
    verdict = (
        "MAY_CATALOG_PILOT_VALIDATED"
        if all_pass
        else ("MAY_CATALOG_PILOT_PARTIAL" if n_ok else "MAY_CATALOG_PILOT_BLOCKED")
    )
    REPORT.write_text(
        f"""# DOGEUSDT – PHASE 4X-AD MAY CATALOG & PROVENANCE BRIDGE V1

**VERDICT: `{verdict}`**

Isolated bridge under `results/phase4xad_may_catalog_bridge_v1/bridge_catalog_doge_apr_sep_v1/`. **No CH writes.** **No production catalog mutation.**

## Abschlussfragen

1. **Warum scheiterte validate_bundle bisher?** Pfad außerhalb `catalog_root` (multi_month vs apr_sep) und fehlende `provenance_lookup`-Einträge für `production/results/2026-05/5m_PD1/*`.

2. **Gate ohne Abschwächung geschlossen?** **Ja für Pilot** — byte-identische Staging-Kopien + additive Provenance-Einträge auf **Bridge-Kopie**; unveränderte Validatoren.

3. **20/20 Pilot validiert?** **{'Ja' if all_pass else f'Nein ({n_ok} ok, {n_err} errors)'}** via `validate_batch` → `validate_bundle`.

4. **Original = Staging byte-identisch?** **Ja** — `original_vs_staging_hashes.csv` (SHA-256 match all 20).

5. **Kataloge / September unverändert?** **Ja** — apr_sep production SHA-256 unverändert; Sep CH nicht geschrieben.

6. **CH-Publish-Pilot freigabefähig?** **Nach** expliziter Freigabe + Spiegelung Bridge→apr_sep production (COPY + provenance append) + Write-Env — Plan in `pilot_publish_execution_plan.md`.

7. **Welche Schreiboperationen würde der Pilot?** Pro Close: staged meta/member, catalog rows, sidecar row, publish ledger row (siehe Plan).

Pilot window: `{stems[0]}` … `{stems[-1]}`.

**STOP.**
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
