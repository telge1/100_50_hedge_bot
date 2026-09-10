"""Builder configuration — absolute read-only data paths, no known-episode calc inputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..wall_defense_outcome_contract_v1 import ATTACK_CLUSTER_GAP_MS_DEFAULT
from . import (
    BOOK_SOURCE,
    EXPECTED_CONTRACT_HASH,
    RUN_PREFIX,
    SCHEMA_VERSION,
    WORKTREE_ROOT,
)

# Absolute read-only sources (document only; do not copy raw data).
DEFAULT_MP_EVENTS = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/"
    "bounded_level_first_analyzer_pilot_v1/BTCUSDT/lf1_69e21d12d280596e/mp_level_events.csv"
)
DEFAULT_LEVEL_CLUSTERS = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/"
    "bounded_level_first_analyzer_pilot_v1/BTCUSDT/lf1_69e21d12d280596e/level_clusters.csv"
)
DEFAULT_TRADES = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/"
    "level_first_episode1_corrected_sms1_persist_v1/BTCUSDT/"
    "episode1_independent_derivation_inputs_v1/public_trades_zone_window.jsonl"
)
DEFAULT_CANDLES = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/"
    "level_first_episode1_corrected_sms1_persist_v1/BTCUSDT/"
    "episode1_independent_derivation_inputs_v1/candles_1m_zone_window.jsonl"
)
DEFAULT_FULL_OB_ARCHIVE = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_v1"
)
DEFAULT_EXISTING_PERSIST = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/"
    "level_first_episode1_wall_flow_qdh_base_v1/BTCUSDT/wfq1_58db1918314881b6a"
)

RESULTS_ROOT = Path(WORKTREE_ROOT) / "obfull_research_engine/results/btc_30m_generic_defense_episode_builder_v1"

# Keys that must never appear as calculation inputs.
FORBIDDEN_CALC_INPUT_KEYS = (
    "research_visit_count",
    "FIRST_TOUCH_ISO",
    "first_touch_iso",
    "known_episode_id",
    "known_touch_time",
    "episode1_first_touch",
    "EPISODE1_FIRST_TOUCH",
)


@dataclass
class BuilderConfig:
    symbol: str = "BTCUSDT"
    window_start: str = "2026-09-06T19:00:00Z"
    window_end: str = "2026-09-06T23:00:00Z"
    tick_size: float = 0.1
    band_ticks: int = 5
    max_pilot_clusters: int = 20
    attack_cluster_gap_ms: int = ATTACK_CLUSTER_GAP_MS_DEFAULT
    wall_distance_cap_ticks: int = 50
    wall_min_notional_heuristic: float = 0.0
    book_source: str = BOOK_SOURCE
    mp_events_path: str = str(DEFAULT_MP_EVENTS)
    level_clusters_path: str = str(DEFAULT_LEVEL_CLUSTERS)
    trades_path: str = str(DEFAULT_TRADES)
    candles_path: str = str(DEFAULT_CANDLES)
    full_ob_archive: str = str(DEFAULT_FULL_OB_ARCHIVE)
    existing_persist_fallback: str = str(DEFAULT_EXISTING_PERSIST)
    out_root: str = str(RESULTS_ROOT)
    run_key: str | None = None
    pilot: bool = False
    expected_contract_hash: str = EXPECTED_CONTRACT_HASH
    schema_version: str = SCHEMA_VERSION
    run_prefix: str = RUN_PREFIX
    # Explicitly NOT selection inputs — listed for audit/documentation only.
    note_no_visit_count_selector: str = (
        "research_visit_count is NOT used as a selector in the generic builder"
    )
    note_no_known_episode_ids: str = (
        "No FIRST_TOUCH_ISO / known episode IDs / known touch times as calc inputs"
    )
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Ensure forbidden keys never sneak into calc config via extra.
        for k in FORBIDDEN_CALC_INPUT_KEYS:
            d.pop(k, None)
            if isinstance(d.get("extra"), dict):
                d["extra"].pop(k, None)
        return d

    def source_manifest(self) -> dict[str, Any]:
        return {
            "mp_events_path": self.mp_events_path,
            "level_clusters_path": self.level_clusters_path,
            "trades_path": self.trades_path,
            "candles_path": self.candles_path,
            "full_ob_archive": self.full_ob_archive,
            "existing_persist_fallback": self.existing_persist_fallback,
            "book_source": self.book_source,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "symbol": self.symbol,
            "attack_cluster_gap_ms": self.attack_cluster_gap_ms,
            "tick_size": self.tick_size,
            "band_ticks": self.band_ticks,
            "max_pilot_clusters": self.max_pilot_clusters,
            "expected_contract_hash": self.expected_contract_hash,
            "schema_version": self.schema_version,
        }

    def out_dir(self) -> Path:
        key = self.run_key or f"{self.run_prefix}pending"
        return Path(self.out_root) / self.symbol / key


def assert_no_forbidden_calc_inputs(cfg: dict[str, Any] | BuilderConfig | None) -> list[str]:
    raw = cfg.to_dict() if isinstance(cfg, BuilderConfig) else dict(cfg or {})
    found: list[str] = []
    stack: list[Any] = [raw]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in FORBIDDEN_CALC_INPUT_KEYS:
                    found.append(k)
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    if found:
        raise ValueError(f"forbidden calc inputs present: {sorted(set(found))}")
    return found


def load_config_json(path: Path | str) -> BuilderConfig:
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert_no_forbidden_calc_inputs(data)
    known = {f.name for f in BuilderConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in data.items() if k in known}
    return BuilderConfig(**kwargs)
