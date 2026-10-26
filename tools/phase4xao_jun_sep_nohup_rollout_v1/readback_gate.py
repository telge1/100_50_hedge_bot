"""Month-aware 4X-AK optimized readback."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_OUT = _ROOT / "results/phase4xao_jun_sep_nohup_rollout_v1"
_AK = _ROOT / "tools/phase4xak_density_readback_optimization_v1/run_phase4xak.py"
_APR_SEP = (
    _ROOT
    / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
)
_COMBO = "5m_PD1"
_PIPELINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"


def _load_ak():
    name = "phase4xak_jun_sep"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _AK)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def parity_index(month: str) -> dict[str, dict[str, str]]:
    path = _OUT / "month_validations" / month / "parity_index.json"
    if not path.is_file():
        raise FileNotFoundError(f"missing validation manifest {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def optimized_readback_batch(
    hcfg: Any,
    month: str,
    stems: list[str],
    density_ref: dict[str, dict[str, str]],
    cache: Any,
    client: Any,
    database: str,
    reader: Any,
) -> list[dict[str, Any]]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from import_path_5m_pd1 import ensure_5m_pd1_readback_import_path

    ensure_5m_pd1_readback_import_path()
    ak = _load_ak()
    from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity import (
        compare_json_to_published,
    )

    rows: list[dict[str, Any]] = []
    density_jobs: list[tuple[str, dict, str]] = []

    for stem in stems:
        path = _APR_SEP / "production/results" / month / _COMBO / f"{stem}.json"
        frozen = json.loads(path.read_text(encoding="utf-8"))
        close_iso = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        dc = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        rep = compare_json_to_published(
            path, catalog_root=_APR_SEP, pipeline_line_id=_PIPELINE, config_hash_value=hcfg.config_hash
        )
        pub = cache.publication(hcfg.config_hash, dc)
        catalog = cache.catalog(database, pub["snapshot_id"], dc)
        cat_for_payload = [
            {"pool_id": r["pool_id"], "side": r["side"], "bottom_price": r["bottom"], "top_price": r["top"]}
            for r in catalog
        ]
        ch_payload = ak._payload_from_pub(pub, cat_for_payload)
        ch_payload["engine_input_hash"] = pub.get("engine_input_hash") or frozen.get("engine_input_hash")
        ch_payload["active_state_fingerprint"] = pub.get("active_state_fingerprint") or frozen.get(
            "active_state_fingerprint"
        )
        st = load_historical_pool_state_at_close(
            close_iso,
            pool_detail=1,
            timeframe="5m",
            client=client,
            database=database,
            reader=reader,
            cfg_hash=hcfg.config_hash,
            include_remaining=False,
        )
        density_jobs.append((f"{stem}|frozen", frozen, close_iso))
        density_jobs.append((f"{stem}|ch", ch_payload, close_iso))
        ref = density_ref.get(stem, {}).get("frozen_density_manifest", "")
        active_errs = ak.compare_active(frozen, ch_payload)
        rows.append(
            {
                "stem": stem,
                "month": month,
                "json_ch_ok": rep.ok,
                "reader_ok": st.snapshot_id == pub["snapshot_id"],
                "active_ok": not active_errs,
                "active_errors": "|".join(active_errs),
                "density_4xab": ref,
                "_pending_density": True,
            }
        )

    manifests = ak.density_manifests_batched(density_jobs)
    for row in rows:
        stem = row["stem"]
        fm = manifests[f"{stem}|frozen"]
        cm = manifests[f"{stem}|ch"]
        ref = row["density_4xab"]
        row["density_frozen"] = fm
        row["density_ch"] = cm
        row["density_ok"] = fm == cm == ref if ref else fm == cm
        del row["_pending_density"]
    return rows


def gate_ok(rows: list[dict[str, Any]]) -> bool:
    return all(
        r.get("json_ch_ok") and r.get("reader_ok") and r.get("active_ok") and r.get("density_ok") for r in rows
    )
