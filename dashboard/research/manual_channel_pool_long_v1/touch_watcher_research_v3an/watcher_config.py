"""Versioned Touch-Watcher research configuration (isolated V3AN; no production)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

CONFIG_SCHEMA_VERSION = "touch_watcher/v1"

ConfirmationMode = Literal["A1", "A2", "A3", "LEGACY_C1", "B_RECLAIM"]
ALLOWED_TOUCH_TOLERANCE = (0.0, 0.01, 0.02, 0.03, 0.05, 0.20)


@dataclass(frozen=True)
class TouchWatcherConfig:
    """All replay parameters; invalid combinations raise — no silent defaults."""

    schema_version: str = CONFIG_SCHEMA_VERSION
    touch_tolerance_pct: float = 0.03
    confirmation_mode: ConfirmationMode = "A2"
    confirmation_min_gain_pct: float = 0.20
    watch_window_bars: int = 12
    reclaim_confirmation_bars: int = 2
    require_approach_from_above: bool = True
    approach_epsilon_pct: float = 0.05
    sl_pct: float = 1.20
    preset_id: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != CONFIG_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version!r}")
        if self.touch_tolerance_pct < 0:
            raise ValueError("touch_tolerance_pct must be >= 0")
        if self.touch_tolerance_pct > 0.20 + 1e-9:
            raise ValueError("touch_tolerance_pct must be <= 0.20 for L1 research grid")
        if self.confirmation_mode not in ("A1", "A2", "A3", "LEGACY_C1", "B_RECLAIM"):
            raise ValueError(f"unknown confirmation_mode: {self.confirmation_mode}")
        if self.confirmation_min_gain_pct < 0:
            raise ValueError("confirmation_min_gain_pct must be >= 0")
        if self.watch_window_bars < 1:
            raise ValueError("watch_window_bars must be >= 1")
        if self.reclaim_confirmation_bars < 2:
            raise ValueError("reclaim_confirmation_bars must be >= 2 (B scenario)")
        if self.sl_pct <= 0:
            raise ValueError("sl_pct must be positive")

    def config_id(self) -> str:
        blob = json.dumps(self.to_documented_dict(), sort_keys=True)
        h = hashlib.sha256(blob.encode()).hexdigest()[:12]
        if self.preset_id:
            return f"{self.preset_id}#{h}"
        return h

    def to_documented_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("preset_id", None)
        return d

    def legacy_touch_mode_label(self) -> str:
        """Maps to V3AN v1 matrix labels for regression."""
        if abs(self.touch_tolerance_pct - 0.20) < 1e-9:
            return "LEGACY_020"
        if abs(self.touch_tolerance_pct - 0.0) < 1e-9:
            return "PHYSICAL_ONLY"
        if abs(self.touch_tolerance_pct - 0.03) < 1e-9:
            return "NEAR_003"
        return f"TOL_{self.touch_tolerance_pct:.4f}"


def load_touch_watcher_config(path: Path) -> TouchWatcherConfig:
    if yaml is None:
        raise RuntimeError("PyYAML required for config load: pip install pyyaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "touch_watcher" not in raw:
        raise ValueError(f"{path}: expected top-level key touch_watcher")
    tw = raw["touch_watcher"]
    if not isinstance(tw, dict):
        raise ValueError("touch_watcher must be a mapping")
    if "schema_version" in tw and tw["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise ValueError(f"schema_version mismatch: {tw['schema_version']}")
    required = {
        "touch_tolerance_pct",
        "confirmation_mode",
        "confirmation_min_gain_pct",
        "watch_window_bars",
        "reclaim_confirmation_bars",
        "require_approach_from_above",
        "approach_epsilon_pct",
        "sl_pct",
    }
    missing = required - set(tw)
    if missing:
        raise ValueError(f"missing required touch_watcher keys: {sorted(missing)}")
    allowed = {
        "schema_version",
        "touch_tolerance_pct",
        "confirmation_mode",
        "confirmation_min_gain_pct",
        "watch_window_bars",
        "reclaim_confirmation_bars",
        "require_approach_from_above",
        "approach_epsilon_pct",
        "sl_pct",
        "preset_id",
    }
    extra = set(tw) - allowed
    if extra:
        raise ValueError(f"unknown config keys: {sorted(extra)}")
    return TouchWatcherConfig(
        schema_version=tw.get("schema_version", CONFIG_SCHEMA_VERSION),
        touch_tolerance_pct=float(tw["touch_tolerance_pct"]),
        confirmation_mode=tw["confirmation_mode"],
        confirmation_min_gain_pct=float(tw["confirmation_min_gain_pct"]),
        watch_window_bars=int(tw["watch_window_bars"]),
        reclaim_confirmation_bars=int(tw["reclaim_confirmation_bars"]),
        require_approach_from_above=bool(tw["require_approach_from_above"]),
        approach_epsilon_pct=float(tw["approach_epsilon_pct"]),
        sl_pct=float(tw["sl_pct"]),
        preset_id=str(tw.get("preset_id", "")),
    )


def preset_matrix_v1() -> list[TouchWatcherConfig]:
    """Reproduces the original 45-run V3AN grid (touch × confirm × window)."""
    touches = [0.20, 0.03, 0.0]
    touch_ids = ["LEGACY_020", "NEAR_003", "PHYSICAL_ONLY"]
    confirms: list[ConfirmationMode] = ["LEGACY_C1", "A1", "A2", "A3", "B_RECLAIM"]
    windows = [6, 12, 24]
    out: list[TouchWatcherConfig] = []
    for tol, tid in zip(touches, touch_ids):
        for cm in confirms:
            for w in windows:
                out.append(
                    TouchWatcherConfig(
                        touch_tolerance_pct=tol,
                        confirmation_mode=cm,
                        confirmation_min_gain_pct=0.20,
                        watch_window_bars=w,
                        preset_id=f"{tid}+{cm}+W{w}",
                    )
                )
    return out
