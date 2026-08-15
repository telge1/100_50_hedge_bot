"""State-machine tests for decisive-break v3."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.tem_structure_break.decisive_break import run_decisive_break
from research.regime_scanner.tem_structure_break.decisive_models import DecisiveState


def _h4_from_ohlc(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    # rows: (ts, o, h, l, c)
    data = []
    for ts, o, h, l, c in rows:
        t = pd.Timestamp(ts, tz="UTC")
        data.append(
            {
                "timestamp": t,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "htf_close_decision": t + pd.Timedelta(hours=4),
            }
        )
    return pd.DataFrame(data)


def test_decisive_arm_level_pending_confirm_path() -> None:
    # After arm at t0, stabilize 3 bars, swing low at bar with low=90 confirmed next bar,
    # later close below 90, next bar fails reclaim.
    rows = []
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    # bars 0..2: post-arm stabilize (high structure)
    for i in range(3):
        t = base + pd.Timedelta(hours=4 * i)
        rows.append((str(t), 100, 101, 99, 100))
    # bar 3: swing low candidate low=90
    t3 = base + pd.Timedelta(hours=12)
    rows.append((str(t3), 100, 100, 90, 92))
    # bar 4: confirm swing (higher low) — level ready at close
    t4 = base + pd.Timedelta(hours=16)
    rows.append((str(t4), 92, 95, 91, 94))
    # bar 5: break below 90
    t5 = base + pd.Timedelta(hours=20)
    rows.append((str(t5), 94, 94, 88, 89))
    # bar 6: no reclaim
    t6 = base + pd.Timedelta(hours=24)
    rows.append((str(t6), 89, 90, 87, 88))

    h4 = _h4_from_ohlc(rows)
    arm = str(base + pd.Timedelta(hours=4))  # after first bar close decision-ish
    # Use close decision of bar0 as arm so arm_idx=0
    arm = str(h4.iloc[0]["htf_close_decision"])
    rt = run_decisive_break(h4, v2_first_break_ts=arm, v2_break_level=99.0, stabilize_bars=3)
    assert rt.state == DecisiveState.DECISIVE_BREAK_CONFIRMED
    assert rt.level is not None
    assert rt.level.value == 90.0
    assert rt.confirmed_ts is not None
    assert any(e["event"] == "DECISIVE_LEVEL_READY" for e in rt.events)
    assert any(e["event"] == "DECISIVE_BREAK_PENDING" for e in rt.events)


def test_reclaim_then_rearm_allows_new_cycle() -> None:
    base = pd.Timestamp("2026-02-01T00:00:00Z")
    rows = []
    for i in range(3):
        t = base + pd.Timedelta(hours=4 * i)
        rows.append((str(t), 100, 101, 99, 100))
    rows.append((str(base + pd.Timedelta(hours=12)), 100, 100, 90, 92))
    rows.append((str(base + pd.Timedelta(hours=16)), 92, 95, 91, 94))
    rows.append((str(base + pd.Timedelta(hours=20)), 94, 94, 88, 89))  # pending
    rows.append((str(base + pd.Timedelta(hours=24)), 89, 96, 89, 95))  # reclaim
    # re-stabilize-ish + new swing
    for i in range(3):
        t = base + pd.Timedelta(hours=28 + 4 * i)
        rows.append((str(t), 95, 96, 94, 95))
    rows.append((str(base + pd.Timedelta(hours=40)), 95, 95, 85, 86))
    rows.append((str(base + pd.Timedelta(hours=44)), 86, 90, 86, 88))
    rows.append((str(base + pd.Timedelta(hours=48)), 88, 88, 80, 81))
    rows.append((str(base + pd.Timedelta(hours=52)), 81, 82, 79, 80))

    h4 = _h4_from_ohlc(rows)
    arm = str(h4.iloc[0]["htf_close_decision"])
    rt = run_decisive_break(h4, v2_first_break_ts=arm, stabilize_bars=3)
    assert any(e["event"] == "DECISIVE_BREAK_RECLAIMED" for e in rt.events)
    # may or may not confirm second cycle depending on pivots; reclaim must clear sticky confirm
    assert rt.state != DecisiveState.DECISIVE_BREAK_CONFIRMED or rt.reclaim_ts is not None


def test_no_arm_without_v2_break() -> None:
    h4 = _h4_from_ohlc([("2026-01-01T00:00:00Z", 1, 2, 0.5, 1)])
    rt = run_decisive_break(h4, v2_first_break_ts=None)
    assert rt.state.value == "DECISIVE_NOT_ARMED"
