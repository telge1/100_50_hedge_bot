"""Load and fail-closed validate BT-D4 research backtester config."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path(__file__).resolve().parent / "research_config_v1.json"

REQUIRED_MODE = "depth2_density_research_v1"
REQUIRED_DENSITY_METHOD = "exact_unique_origin"
REQUIRED_TIMEFRAME = "5m"
REQUIRED_POOL_DETAIL = 1
REQUIRED_MIN_ORIGINS = 2


class ResearchConfigError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class ResearchBacktesterConfigV1:
    mode: str
    mode_version: str
    symbol: str
    timeframe: str
    pool_detail: int
    pool_pd: str
    density_method: str
    min_unique_origins: int
    warmup_hours: int
    expected_pipeline_line_id: str
    golden_reference_path: Path
    parity_fixture_decision_closes: tuple[str, ...]
    additional_parity_closes: tuple[str, ...]
    causal_sequence_closes: tuple[str, ...]
    adapter_bridge: str
    density_provider_module: str
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path | None = None) -> ResearchBacktesterConfigV1:
        p = path or DEFAULT_CONFIG
        if not p.is_file():
            raise ResearchConfigError("CONFIG_MISSING", f"research config not found: {p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(raw, source_path=p)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source_path: Path | None = None) -> ResearchBacktesterConfigV1:
        missing = [k for k in (
            "mode", "symbol", "timeframe", "pool_detail", "density_method", "min_unique_origins",
            "expected_pipeline_line_id", "golden_reference_path",
        ) if k not in raw]
        if missing:
            raise ResearchConfigError(
                "CONFIG_INCOMPLETE",
                "missing required research config keys",
                {"missing": missing},
            )
        cfg = cls(
            mode=str(raw["mode"]),
            mode_version=str(raw.get("mode_version", "bt_d4_research_v1")),
            symbol=str(raw["symbol"]),
            timeframe=str(raw["timeframe"]),
            pool_detail=int(raw["pool_detail"]),
            pool_pd=str(raw.get("pool_pd", "PD1")),
            density_method=str(raw["density_method"]),
            min_unique_origins=int(raw["min_unique_origins"]),
            warmup_hours=int(raw.get("warmup_hours", 168)),
            expected_pipeline_line_id=str(raw["expected_pipeline_line_id"]),
            golden_reference_path=(ROOT / raw["golden_reference_path"]).resolve(),
            parity_fixture_decision_closes=tuple(raw.get("parity_fixture_decision_closes") or []),
            additional_parity_closes=tuple(raw.get("additional_parity_closes") or []),
            causal_sequence_closes=tuple(raw.get("causal_sequence_closes") or []),
            adapter_bridge=str(raw.get("adapter_bridge", "")),
            density_provider_module=str(raw.get("density_provider_module", "")),
            raw=raw,
        )
        cfg.enforce_contract()
        return cfg

    def enforce_contract(self) -> None:
        if self.mode != REQUIRED_MODE:
            raise ResearchConfigError(
                "MODE_INVALID",
                f"research mode must be {REQUIRED_MODE}",
                {"got": self.mode},
            )
        if self.density_method != REQUIRED_DENSITY_METHOD:
            raise ResearchConfigError("DENSITY_METHOD_MISMATCH", self.density_method)
        if self.timeframe != REQUIRED_TIMEFRAME:
            raise ResearchConfigError("TIMEFRAME_NOT_SUPPORTED", self.timeframe)
        if self.pool_detail != REQUIRED_POOL_DETAIL:
            raise ResearchConfigError("POOL_DETAIL_NOT_SUPPORTED", str(self.pool_detail))
        if self.min_unique_origins != REQUIRED_MIN_ORIGINS:
            raise ResearchConfigError(
                "MIN_UNIQUE_ORIGINS_MISMATCH",
                f"must be {REQUIRED_MIN_ORIGINS}",
                {"got": self.min_unique_origins},
            )
        if self.min_unique_origins not in (2,):
            raise ResearchConfigError("DEPTH_NOT_EXACTLY_TWO", str(self.min_unique_origins))

    def manifest_base(self) -> dict[str, Any]:
        return {
            "research_mode": self.mode,
            "research_mode_version": self.mode_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "pool_detail": self.pool_detail,
            "pool_pd": self.pool_pd,
            "density_method": self.density_method,
            "min_unique_origins": self.min_unique_origins,
            "exact_unique_origin_density": True,
            "warmup_hours": self.warmup_hours,
            "adapter_bridge": self.adapter_bridge,
            "density_provider_module": self.density_provider_module,
            "expected_pipeline_line_id": self.expected_pipeline_line_id,
            "golden_reference_path": str(self.golden_reference_path),
            "fail_closed": True,
        }
