"""Canonical chart pool as-of restore — adapter, dashboard pane, guardrail, EXP_04."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
OA_SRC = OA_ROOT / "src"
DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
if str(OA_SRC) not in sys.path:
    sys.path.insert(0, str(OA_SRC))
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

from orderbook_analyse.liquidity_pool_signal import (  # noqa: E402
    CANONICAL_PROVIDER_VERSION,
    POOL_ARRIVALS_CSV_NOT_POOL_SOURCE_OF_TRUTH,
    CanonicalPoolParityError,
    assert_canonical_pool_parity,
    causal_pane_lld_bundle,
    chart_pool_engine,
    export_snapshot,
    get_engine_function,
    parse_as_of_iso,
)
from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import fingerprint  # noqa: E402
from orderbook_analyse.liquidity_pool_signal.canonical import (  # noqa: E402
    VERDICT_BOUNDS_MISMATCH,
    VERDICT_POOL_NOT_VISIBLE,
    VERDICT_SIDE_MISMATCH,
    VERDICT_TIMEFRAME_MISMATCH,
    clip_overlays_to_as_of,
)

EXP_POOL = "lld:BTCUSDT:5m:lower:1787740200"
CONTACT = "2026-08-26T11:34:51Z"
ENTRY = "2026-08-26T11:37:17Z"
LOWER = 78475.5
UPPER = 78526.2
BID_COLOR = "#228bab"


def _as_of(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _pool_from_snap(snap: dict, pool_id: str) -> dict | None:
    for p in snap.get("active_canonical_pools") or snap.get("active_pools") or []:
        if p["pool_id"] == pool_id:
            return p
    return None


def _overlay_for_pool(overlays: list[dict], pool_id: str) -> dict | None:
    zone = f"{pool_id}:zone"
    for o in overlays:
        if o.get("id") == zone:
            return o
        meta = o.get("metadata") or {}
        if meta.get("pool_id") == pool_id:
            return o
    return None


def test_foundation_engine_identity_unchanged():
    eng = get_engine_function()
    assert eng is chart_pool_engine()
    from research_charts.trp_import import load_trp

    assert eng is load_trp()["run_liquidity_location"]


def test_parse_as_of_iso_valid_and_invalid():
    dt = parse_as_of_iso(CONTACT)
    assert dt == _as_of(CONTACT)
    with pytest.raises(ValueError, match="invalid_liquidity_location_as_of"):
        parse_as_of_iso("not-a-timestamp")


def test_pool_arrivals_not_source_of_truth_flag():
    assert POOL_ARRIVALS_CSV_NOT_POOL_SOURCE_OF_TRUTH is True


@pytest.mark.parametrize("as_of_s", [CONTACT, ENTRY])
def test_exp04_canonical_snapshot_contains_pool(as_of_s: str):
    snap = export_snapshot(
        symbol="BTCUSDT",
        timeframe="5m",
        window_start=_as_of(as_of_s),
        as_of=_as_of(as_of_s),
    )
    pool = _pool_from_snap(snap, EXP_POOL)
    assert pool is not None
    assert pool["side"] == "BID"
    assert pool["lower"] == LOWER
    assert pool["upper"] == UPPER
    assert pool["active_as_of"] is True
    assert pool["canonical_provider_version"] == CANONICAL_PROVIDER_VERSION
    assert snap["canonical_snapshot_sha256"]


def test_historical_asof_excludes_future_start_overlays():
    snap = export_snapshot(
        symbol="BTCUSDT",
        timeframe="5m",
        window_start=_as_of(CONTACT),
        as_of=_as_of(CONTACT),
    )
    as_of_unix = int(_as_of(CONTACT).timestamp())
    bundle = causal_pane_lld_bundle(
        symbol="BTCUSDT", timeframe="5m", as_of=_as_of(CONTACT)
    )
    for o in bundle["overlays"]:
        meta = o.get("metadata") or {}
        if meta.get("source") not in ("lld", "lld-cluster"):
            continue
        st = o.get("start_timestamp")
        assert st is None or int(st) <= as_of_unix
        assert o.get("extend_right") is False
        en = o.get("end_timestamp")
        assert en is not None and int(en) <= as_of_unix


def test_pane_causal_vs_default_end_now():
    from research_charts.service import pane_bundle

    now = int(datetime.now(timezone.utc).timestamp())
    ref = int(_as_of(CONTACT).timestamp())
    start = ref - 86400 * 3
    default = pane_bundle(
        "BTCUSDT",
        "5m",
        start=start,
        end=now,
        liquidity={"enabled": True},
    )
    causal = pane_bundle(
        "BTCUSDT",
        "5m",
        start=start,
        end=now,
        liquidity={"enabled": True},
        liquidity_location_as_of=CONTACT,
    )
    assert causal.get("liquidity_location_mode") == "causal_as_of"
    assert causal.get("liquidity_location_as_of") == CONTACT
    assert default.get("liquidity_location_mode") in (None, "live")

    def _has_pool(payload: dict) -> bool:
        for o in payload.get("overlays") or []:
            meta = o.get("metadata") or {}
            pid = meta.get("pool_id") or o.get("id") or ""
            if EXP_POOL in str(pid):
                return True
        return False

    assert _has_pool(causal) is True


def test_pane_adapter_pool_parity_sha():
    snap = export_snapshot(
        symbol="BTCUSDT",
        timeframe="5m",
        window_start=_as_of(CONTACT),
        as_of=_as_of(CONTACT),
    )
    from research_charts.service import pane_bundle

    ref = int(_as_of(CONTACT).timestamp())
    pane = pane_bundle(
        "BTCUSDT",
        "5m",
        start=ref - 86400,
        end=ref + 3600,
        liquidity={"enabled": True},
        liquidity_location_as_of=CONTACT,
    )
    adapter_pool = _pool_from_snap(snap, EXP_POOL)
    pane_ov = _overlay_for_pool(pane.get("overlays") or [], EXP_POOL)
    assert adapter_pool is not None
    assert pane_ov is not None
    assert float(pane_ov["bottom_price"]) == adapter_pool["lower"]
    assert float(pane_ov["top_price"]) == adapter_pool["upper"]
    assert (pane_ov.get("style") or {}).get("color") == BID_COLOR

    adapter_norm = [
        {
            "pool_id": adapter_pool["pool_id"],
            "side": adapter_pool["side"],
            "lower": adapter_pool["lower"],
            "upper": adapter_pool["upper"],
            "available_at": adapter_pool["available_at"],
            "active_as_of": adapter_pool["active_as_of"],
        }
    ]
    pane_pool_norm = [
        {
            "pool_id": EXP_POOL,
            "side": "BID",
            "lower": float(pane_ov["bottom_price"]),
            "upper": float(pane_ov["top_price"]),
            "available_at": adapter_pool["available_at"],
            "active_as_of": True,
        }
    ]
    assert fingerprint({"pools": adapter_norm}) == fingerprint({"pools": pane_pool_norm})
    assert pane.get("canonical_snapshot_sha256") == snap["canonical_snapshot_sha256"]


def test_guardrail_accepts_exact_parity():
    snap = export_snapshot(
        symbol="BTCUSDT",
        timeframe="5m",
        window_start=_as_of(CONTACT),
        as_of=_as_of(CONTACT),
    )
    case = {
        "pool_id": EXP_POOL,
        "pool_side": "BID",
        "pool_timeframe": "5m",
        "pool_lower": LOWER,
        "pool_upper": UPPER,
    }
    out = assert_canonical_pool_parity(case, snap, as_of=CONTACT)
    assert out["ok"] is True


def test_guardrail_blocks_missing_pool():
    snap = export_snapshot(
        symbol="BTCUSDT",
        timeframe="5m",
        window_start=_as_of(CONTACT),
        as_of=_as_of(CONTACT),
    )
    with pytest.raises(CanonicalPoolParityError) as exc:
        assert_canonical_pool_parity(
            {"pool_id": "lld:BTCUSDT:5m:lower:999", "pool_side": "BID", "pool_lower": 1, "pool_upper": 2},
            snap,
            as_of=CONTACT,
        )
    assert exc.value.verdict == VERDICT_POOL_NOT_VISIBLE


def test_guardrail_blocks_bounds_mismatch():
    snap = export_snapshot(
        symbol="BTCUSDT",
        timeframe="5m",
        window_start=_as_of(CONTACT),
        as_of=_as_of(CONTACT),
    )
    with pytest.raises(CanonicalPoolParityError) as exc:
        assert_canonical_pool_parity(
            {
                "pool_id": EXP_POOL,
                "pool_side": "BID",
                "pool_timeframe": "5m",
                "pool_lower": 1.0,
                "pool_upper": UPPER,
            },
            snap,
            as_of=CONTACT,
        )
    assert exc.value.verdict == VERDICT_BOUNDS_MISMATCH


def test_guardrail_blocks_side_mismatch():
    snap = export_snapshot(
        symbol="BTCUSDT",
        timeframe="5m",
        window_start=_as_of(CONTACT),
        as_of=_as_of(CONTACT),
    )
    with pytest.raises(CanonicalPoolParityError) as exc:
        assert_canonical_pool_parity(
            {
                "pool_id": EXP_POOL,
                "pool_side": "ASK",
                "pool_timeframe": "5m",
                "pool_lower": LOWER,
                "pool_upper": UPPER,
            },
            snap,
            as_of=CONTACT,
        )
    assert exc.value.verdict == VERDICT_SIDE_MISMATCH


def test_guardrail_blocks_timeframe_mismatch():
    snap = export_snapshot(
        symbol="BTCUSDT",
        timeframe="5m",
        window_start=_as_of(CONTACT),
        as_of=_as_of(CONTACT),
    )
    with pytest.raises(CanonicalPoolParityError) as exc:
        assert_canonical_pool_parity(
            {
                "pool_id": EXP_POOL,
                "pool_side": "BID",
                "pool_timeframe": "15m",
                "pool_lower": LOWER,
                "pool_upper": UPPER,
            },
            snap,
            as_of=CONTACT,
        )
    assert exc.value.verdict == VERDICT_TIMEFRAME_MISMATCH


def test_pane_invalid_as_of_fail_closed():
    from research_charts.service import pane_bundle

    with pytest.raises(ValueError, match="invalid_liquidity_location_as_of"):
        pane_bundle(
            "BTCUSDT",
            "5m",
            liquidity={"enabled": True},
            liquidity_location_as_of="bad-ts",
        )


def test_clip_overlays_preserves_visible_at_earlier_asof():
    overlays = [
        {
            "id": "lld:BTCUSDT:5m:lower:1787740200:zone",
            "start_timestamp": 1787740800,
            "end_timestamp": None,
            "extend_right": True,
            "metadata": {"source": "lld", "pool_id": EXP_POOL},
        }
    ]
    early = clip_overlays_to_as_of(overlays, int(_as_of(CONTACT).timestamp()))
    assert len(early) == 1
    assert early[0]["extend_right"] is False
