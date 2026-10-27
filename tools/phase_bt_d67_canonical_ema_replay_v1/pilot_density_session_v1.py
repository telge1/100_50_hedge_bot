"""Pilot density session — D54 / D8 cache / D4 canonical (no alternate math)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

for p in (
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from density_bridge_v1 import create_density_session  # noqa: E402
from d54_snapshot_loader_v1 import load_d54_window_snapshots  # noqa: E402


class PilotDensitySessionV1:
    """
    Read-only layered session:
    - d54 frozen snapshots (canonical collect artifacts)
    - validated_cache (BT-D8/D9 bundle)
    - ResearchDensityRunV1.collect_at_close for remaining bars (cached in-memory)
    """

    def __init__(
        self,
        *,
        base_config: dict[str, Any],
        pilot: dict[str, Any],
        canonical_session: Any,
        cache_session: Any | None,
        d54_snapshots: dict[str, dict[str, Any]],
    ) -> None:
        self.pilot = pilot
        self._canonical = canonical_session
        self._cache = cache_session
        self._d54 = d54_snapshots
        self._mem: dict[str, dict[str, Any]] = {}
        self.provenance = {
            "pilot_id": pilot["pilot_id"],
            "density_mode": pilot["density_mode"],
            "d54_bars": len(d54_snapshots),
            "cache_bundle": pilot.get("cache_bundle"),
        }

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        if decision_close_utc in self._mem:
            return self._mem[decision_close_utc]
        if decision_close_utc in self._d54:
            snap = dict(self._d54[decision_close_utc])
            snap["density_transport"] = "d54_frozen_snapshot"
            self._mem[decision_close_utc] = snap
            return snap
        if self._cache is not None:
            try:
                snap = self._cache.collect_at_close(decision_close_utc)
                snap = dict(snap)
                snap["density_transport"] = "validated_cache"
                self._mem[decision_close_utc] = snap
                return snap
            except Exception:
                pass
        snap = dict(self._canonical.collect_at_close(decision_close_utc))
        snap["density_transport"] = "canonical_provider"
        self._mem[decision_close_utc] = snap
        return snap


def build_pilot_density_session(base_config: dict[str, Any], pilot: dict[str, Any]) -> PilotDensitySessionV1:
    cfg_canon = copy.deepcopy(base_config)
    cfg_canon["density"]["density_source"] = "canonical_provider"
    canonical = create_density_session(cfg_canon)

    cache_session = None
    bundle = pilot.get("cache_bundle")
    if bundle:
        bundle_path = ROOT / bundle
        if bundle_path.is_dir():
            cfg_cache = copy.deepcopy(base_config)
            cfg_cache["density"]["density_source"] = "validated_cache"
            cfg_cache["density"]["validated_cache"] = {"bundle_path": str(bundle_path.resolve())}
            cache_session = create_density_session(cfg_cache)

    d54: dict[str, dict[str, Any]] = {}
    for rel in pilot.get("d54_artifacts") or []:
        p = ROOT / rel
        if p.is_file():
            d54.update(load_d54_window_snapshots(p))

    return PilotDensitySessionV1(
        base_config=base_config,
        pilot=pilot,
        canonical_session=canonical,
        cache_session=cache_session,
        d54_snapshots=d54,
    )


def preflight_coverage(
    session: PilotDensitySessionV1,
    bars: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bar in bars:
        status = "OK"
        transport = ""
        err = ""
        try:
            snap = session.collect_at_close(bar)
            transport = str(snap.get("density_transport") or "")
            if not snap.get("upper_bands") and not snap.get("lower_bands"):
                status = "INCOMPLETE"
                err = "empty_bands"
        except Exception as exc:  # noqa: BLE001
            status = "INCOMPLETE"
            err = str(exc)[:200]
        rows.append(
            {
                "decision_close_utc": bar,
                "coverage_status": status,
                "density_transport": transport,
                "error": err,
            }
        )
    return rows
