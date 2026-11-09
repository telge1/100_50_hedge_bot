"""V3AP — full watcher parameter grid (V3AN config)."""

from __future__ import annotations

from watcher_config import CONFIG_SCHEMA_VERSION, TouchWatcherConfig

TOUCH_GRID = (0.0, 0.01, 0.02, 0.03, 0.05, 0.20)
WINDOW_GRID = (6, 12, 24)
A3_GAIN_GRID = (0.05, 0.10, 0.15, 0.20)
B_RECLAIM_GRID = (2, 3)


def _tol_label(tol: float) -> str:
    if abs(tol) < 1e-9:
        return "TOL0"
    return f"TOL{tol:.2f}".replace(".", "p")


def v3ap_watcher_matrix() -> list[TouchWatcherConfig]:
    out: list[TouchWatcherConfig] = []
    for tol in TOUCH_GRID:
        tl = _tol_label(tol)
        for w in WINDOW_GRID:
            for mode in ("A1", "A2"):
                out.append(
                    TouchWatcherConfig(
                        schema_version=CONFIG_SCHEMA_VERSION,
                        touch_tolerance_pct=tol,
                        confirmation_mode=mode,
                        confirmation_min_gain_pct=0.20,
                        watch_window_bars=w,
                        reclaim_confirmation_bars=2,
                        preset_id=f"V3AP_{tl}+{mode}+W{w}",
                    )
                )
            for gain in A3_GAIN_GRID:
                out.append(
                    TouchWatcherConfig(
                        schema_version=CONFIG_SCHEMA_VERSION,
                        touch_tolerance_pct=tol,
                        confirmation_mode="A3",
                        confirmation_min_gain_pct=gain,
                        watch_window_bars=w,
                        reclaim_confirmation_bars=2,
                        preset_id=f"V3AP_{tl}+A3g{gain:.2f}+W{w}".replace(".", "p"),
                    )
                )
            for rb in B_RECLAIM_GRID:
                out.append(
                    TouchWatcherConfig(
                        schema_version=CONFIG_SCHEMA_VERSION,
                        touch_tolerance_pct=tol,
                        confirmation_mode="B_RECLAIM",
                        confirmation_min_gain_pct=0.20,
                        watch_window_bars=w,
                        reclaim_confirmation_bars=rb,
                        preset_id=f"V3AP_{tl}+B_R{rb}+W{w}",
                    )
                )
    return out
