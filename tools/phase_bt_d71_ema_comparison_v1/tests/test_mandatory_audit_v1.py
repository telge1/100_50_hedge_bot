"""EMA mandatory audit expectations (OHLC optional)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parents[1]
for p in (
    PKG,
    ROOT / "tools/phase_downtrend_isolated_upper_short_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
):
    sys.path.insert(0, str(p))

from pure_ema_series_provider_v1 import PureEmaSeriesProviderV1  # noqa: E402
from replay_windows_v1 import MANDATORY_EMA_AUDIT  # noqa: E402


def test_mandatory_ema_v1_ok_v3_block_if_d60_present():
    manifest = ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json"
    if not manifest.is_file():
        return
    if json.loads(manifest.read_text()).get("verdict") != "EMA_HISTORY_VALIDATED":
        return
    from datetime import timedelta

    from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch
    from validation_config_v1 import REFERENCE_WARMUP_HOURS

    lo = _parse_utc("2026-08-01T00:00:00Z") - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    keys, prices, _ = load_5m_ohlc_ch(_iso(lo), "2026-09-30T00:00:00Z")
    v1 = PureEmaSeriesProviderV1(bar_keys=keys, prices=prices, classifier_version="v1")
    v3 = PureEmaSeriesProviderV1(bar_keys=keys, prices=prices, classifier_version="v3")
    for row in MANDATORY_EMA_AUDIT:
        ts = row["decision_close_utc"]
        assert v1.confirm_at_close(ts)["ema_structure_decision"] == "SHORT_OK"
        assert v3.confirm_at_close(ts)["ema_structure_decision"] != "SHORT_OK"
