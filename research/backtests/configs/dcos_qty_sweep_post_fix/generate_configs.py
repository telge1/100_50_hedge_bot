"""Generate DCOS qty-sweep post-fix variant configs (backtest-only)."""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent

COMMON = {
    "enabled": True,
    "min_cycle_qty_factor": 0.25,
    "max_short_reduce_distance_pct": 3.0,
    "reachability_guard_enabled": False,
    "target_profit_pct": 0.25,
    "long_add_distance_pct": 0.5,
}


def _band(min_cycle: int, max_cycle: int | None, factor: float) -> dict:
    return {
        "min_cycle_index": min_cycle,
        "max_cycle_index": max_cycle,
        "target_profit_pct": COMMON["target_profit_pct"],
        "cycle_qty_factor": factor,
        "long_add_distance_pct": COMMON["long_add_distance_pct"],
    }


def _write(name: str, *, start_cycle_index: int, bands: list[dict]) -> None:
    payload = {
        "enabled": True,
        "name": name,
        "start_cycle_index": start_cycle_index,
        "min_cycle_qty_factor": COMMON["min_cycle_qty_factor"],
        "max_short_reduce_distance_pct": COMMON["max_short_reduce_distance_pct"],
        "reachability_guard_enabled": COMMON["reachability_guard_enabled"],
        "bands": bands,
    }
    path = CONFIG_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")


VARIANTS: dict[str, tuple[int, list[tuple[int, int | None, float]]]] = {
    "variant_d_very_mild_up": (
        5,
        [(5, 5, 1.02), (6, 6, 1.03), (7, None, 1.05)],
    ),
    "variant_e_tiny_up": (
        5,
        [(5, 5, 1.01), (6, 6, 1.02), (7, None, 1.03)],
    ),
    "variant_f_flat_102": (5, [(5, None, 1.02)]),
    "variant_g_flat_105": (5, [(5, None, 1.05)]),
    "variant_h_late_only_down": (7, [(7, None, 0.90)]),
    "variant_i_late_only_up": (7, [(7, None, 1.05)]),
    "variant_j_cycle5_6_neutral_7_down": (7, [(7, None, 0.85)]),
    "variant_k_mild_down": (
        5,
        [(5, 5, 0.98), (6, 6, 0.95), (7, None, 0.90)],
    ),
    "variant_l_stronger_down": (
        5,
        [(5, 5, 0.95), (6, 6, 0.90), (7, None, 0.85)],
    ),
    "variant_m_flat_095": (5, [(5, None, 0.95)]),
    "variant_n_flat_090": (5, [(5, None, 0.90)]),
    "variant_o_c5_tiny_up_then_down": (
        5,
        [(5, 5, 1.02), (6, 6, 1.00), (7, None, 0.90)],
    ),
    "variant_p_c5_neutral_c6_down": (
        5,
        [(5, 5, 1.00), (6, 6, 0.95), (7, None, 0.90)],
    ),
    "variant_q_c5_down_c6_down": (
        5,
        [(5, 5, 0.98), (6, 6, 0.92), (7, None, 0.85)],
    ),
}


def main() -> None:
    for name, (start, spec) in VARIANTS.items():
        _write(name, start_cycle_index=start, bands=[_band(a, b, f) for a, b, f in spec])
        print(f"wrote {name}.json")
    print(f"Generated {len(VARIANTS)} configs in {CONFIG_DIR}")


if __name__ == "__main__":
    main()
