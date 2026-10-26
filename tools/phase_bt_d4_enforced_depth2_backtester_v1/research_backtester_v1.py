"""
Isolated research backtester mode — enforced Depth-2 density only (no trades).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_enforcement_v1 import ResearchBacktesterConfigV1, ResearchConfigError
from research_density_gateway_v1 import (
    ResearchDensityGatewayError,
    fetch_density_bands_enforced,
    snapshot_payload,
)

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "results" / "phase_bt_d4_enforced_depth2_backtester_v1"


class ResearchBacktesterError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class ResearchDensityRunV1:
    config: ResearchBacktesterConfigV1
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    timing_s: dict[str, float] = field(default_factory=dict)
    run_manifest: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config_path(cls, path: Path | None = None) -> ResearchDensityRunV1:
        cfg = ResearchBacktesterConfigV1.load(path)
        return cls(config=cfg)

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        use_fixture = decision_close_utc in self.config.parity_fixture_decision_closes
        t0 = time.perf_counter()
        result = fetch_density_bands_enforced(
            self.config,
            decision_close_utc,
            use_parity_fixture_window=use_fixture,
        )
        elapsed = time.perf_counter() - t0
        snap = snapshot_payload(result)
        if decision_close_utc in self.snapshots:
            raise ResearchBacktesterError(
                "CAUSAL_STATE_LEAK",
                "decision close already collected in this run",
                {"decision_close_utc": decision_close_utc},
            )
        self.snapshots[decision_close_utc] = snap
        self.timing_s[decision_close_utc] = elapsed
        return snap

    def run_integration_closes(self, closes: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for close in closes:
            out[close] = self.collect_at_close(close)
        return out

    def finalize_manifest(self) -> dict[str, Any]:
        base = self.config.manifest_base()
        manifest = {
            **base,
            "run_kind": "density_research_no_trades",
            "decision_closes_collected": sorted(self.snapshots.keys()),
            "snapshot_count": len(self.snapshots),
            "timing_per_close_s": self.timing_s,
            "timing_total_s": round(sum(self.timing_s.values()), 4),
            "timing_mean_s": round(
                sum(self.timing_s.values()) / len(self.timing_s), 4
            )
            if self.timing_s
            else 0.0,
            "completed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "integration_path": "research_backtester_v1.collect_at_close → research_density_gateway_v1",
        }
        blob = json.dumps({k: v for k, v in manifest.items() if k != "completed_at_utc"}, sort_keys=True)
        manifest["run_manifest_hash"] = hashlib.sha256(blob.encode()).hexdigest()[:16]
        self.run_manifest = manifest
        return manifest

    def write_artifacts(self, run_id: str | None = None) -> Path:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        rid = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = RESULTS_DIR / rid
        out_dir.mkdir(parents=True, exist_ok=True)
        if not self.run_manifest:
            self.finalize_manifest()
        (out_dir / "run_manifest.json").write_text(
            json.dumps(self.run_manifest, indent=2), encoding="utf-8"
        )
        (out_dir / "density_snapshots.json").write_text(
            json.dumps(self.snapshots, indent=2), encoding="utf-8"
        )
        return out_dir


def run_research_session(
    config_path: Path | None = None,
    *,
    include_causal_sequence: bool = True,
) -> ResearchDensityRunV1:
    session = ResearchDensityRunV1.from_config_path(config_path)
    closes = list(session.config.parity_fixture_decision_closes) + list(
        session.config.additional_parity_closes
    )
    if include_causal_sequence:
        for c in session.config.causal_sequence_closes:
            if c not in closes:
                closes.append(c)
    session.run_integration_closes(closes)
    session.finalize_manifest()
    return session
