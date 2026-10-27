from pathlib import Path

from d54_snapshot_loader_v1 import load_d54_window_snapshots

ROOT = Path(__file__).resolve().parents[3]


def test_d54_window_loads_anchor_bar():
    snaps = load_d54_window_snapshots(
        ROOT / "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window1_2026_09_09_1630_1725.json"
    )
    assert "2026-09-09T16:30:00Z" in snaps
    assert len(snaps["2026-09-09T16:30:00Z"]["upper_bands"]) > 0
