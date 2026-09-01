"""Regression: canonical LLD as-of imports OA only after path bootstrap.

Simulates the live dashboard start context:
- CWD = spread_recovery_hedge_short_dev/dashboard
- Python = project .venv
- PYTHONPATH does not initially contain orderbook_analyse/src
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
OA_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
OA_INIT = OA_SRC / "orderbook_analyse" / "__init__.py"
EXPECTED_POOL_ID = "lld:BTCUSDT:5m:lower:1787740200"
CONTACT_AS_OF = "2026-08-26T11:34:51Z"


@pytest.fixture()
def dashboard_cwd(monkeypatch):
    monkeypatch.chdir(DASHBOARD_ROOT)
    if str(DASHBOARD_ROOT) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_ROOT))
    # Drop any prior OA path / imported package so the test matches cold start.
    while str(OA_SRC.resolve()) in sys.path:
        sys.path.remove(str(OA_SRC.resolve()))
    for key in list(sys.modules):
        if key == "orderbook_analyse" or key.startswith("orderbook_analyse."):
            del sys.modules[key]
    return DASHBOARD_ROOT


def test_ensure_oa_on_path_validates_init_and_front_inserts(dashboard_cwd):
    from research_charts.oa_import import ensure_oa_on_path

    assert OA_INIT.is_file()
    assert "orderbook_analyse" not in sys.modules
    root = ensure_oa_on_path()
    assert root == str(OA_SRC.resolve())
    assert sys.path[0] == root
    assert sys.path.count(root) == 1
    # Second call keeps a single front entry.
    ensure_oa_on_path()
    assert sys.path[0] == root
    assert sys.path.count(root) == 1


def test_canonical_lld_import_without_oa_on_pythonpath(dashboard_cwd):
    assert not any(Path(p).resolve() == OA_SRC.resolve() for p in sys.path if p)
    # Module import itself must not require OA on path.
    from research_charts import canonical_lld  # noqa: F401

    assert "orderbook_analyse" not in sys.modules
    from research_charts.canonical_lld import parse_liquidity_location_as_of

    dt = parse_liquidity_location_as_of(CONTACT_AS_OF)
    assert dt.tzinfo is not None
    assert "orderbook_analyse" in sys.modules
    assert sys.path[0] == str(OA_SRC.resolve())


def test_service_causal_path_no_early_oa_import(dashboard_cwd):
    """service.py must not import orderbook_analyse before ensure_oa_on_path."""
    import ast

    src = (DASHBOARD_ROOT / "research_charts" / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Collect ImportFrom targets inside pane_bundle for orderbook_analyse
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "pane_bundle":
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module:
                    assert not child.module.startswith("orderbook_analyse"), (
                        f"pane_bundle must not import {child.module} directly; "
                        "bootstrap via canonical_lld / oa_import first"
                    )


def test_canonical_provider_engine_matches_dashboard_trp(dashboard_cwd):
    from research_charts.oa_import import ensure_oa_on_path
    from research_charts.trp_import import load_trp

    ensure_oa_on_path()
    from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
        get_engine_function,
    )

    trp = load_trp()
    engine = get_engine_function()
    assert engine is trp["run_liquidity_location"]


def test_exp04_pane_bundle_causal_parity(dashboard_cwd):
    from research_charts.oa_import import ensure_oa_on_path
    from research_charts.service import pane_bundle

    ensure_oa_on_path()
    from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import export_snapshot
    from orderbook_analyse.liquidity_pool_signal.canonical import parse_as_of_iso

    as_of = parse_as_of_iso(CONTACT_AS_OF)
    snap = export_snapshot(
        symbol="BTCUSDT",
        timeframe="5m",
        window_start=as_of,
        as_of=as_of,
    )
    adapter_pool = next(
        p for p in snap["active_canonical_pools"] if p["pool_id"] == EXPECTED_POOL_ID
    )
    assert adapter_pool["side"] == "BID"
    assert float(adapter_pool["lower"]) == 78475.5
    assert float(adapter_pool["upper"]) == 78526.2
    assert adapter_pool["active_as_of"] is True

    packed = pane_bundle(
        "BTCUSDT",
        "5m",
        liquidity={"enabled": True},
        liquidity_location_as_of=CONTACT_AS_OF,
        allow_stale=True,
    )
    assert packed.get("liquidity_location_mode") == "causal_as_of"
    assert packed.get("liquidity_location_as_of")
    assert packed.get("canonical_snapshot_sha256") == snap.get("canonical_snapshot_sha256")

    overlays = packed.get("liquidity", {}).get("overlays") or packed.get("overlays") or []
    hit = None
    for row in overlays:
        oid = str(row.get("id") or row.get("pool_id") or "")
        if EXPECTED_POOL_ID in oid or oid == EXPECTED_POOL_ID:
            hit = row
            break
        # overlay may carry price bounds without full pool_id in some serializers
        lo = row.get("price_low", row.get("lower"))
        hi = row.get("price_high", row.get("upper"))
        if lo is not None and hi is not None:
            if abs(float(lo) - 78475.5) < 1e-9 and abs(float(hi) - 78526.2) < 1e-9:
                side = str(row.get("side") or row.get("pool_side") or "").upper()
                if side in ("", "BID", "LOWER", "BUY"):
                    hit = row
                    break
    assert hit is not None, f"expected EXP_04 pool missing from pane overlays ({len(overlays)} overlays)"
