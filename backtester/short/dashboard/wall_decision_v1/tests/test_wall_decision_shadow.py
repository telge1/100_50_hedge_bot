"""Wall Decision V1 — shadow store + API contract tests (no live IO beyond tmp)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1]
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from wall_decision_v1.config import RULE_VERSION, V1_PROVISIONAL  # noqa: E402
from wall_decision_v1.shadow_store import (  # noqa: E402
    append_session_event,
    list_recent_sessions,
    new_session_record,
)


def test_thresholds_are_provisional_and_central():
    assert V1_PROVISIONAL["mark"] == "V1_PROVISIONAL"
    assert V1_PROVISIONAL["wall_consume_pct"] == 0.65
    assert "provisional" in RULE_VERSION


def test_shadow_append_is_jsonl_and_atomic(tmp_path: Path):
    rec = new_session_record(
        session_id="wd1_BTCUSDT_1",
        symbol="BTCUSDT",
        breakpoint=101050.0,
        target_wall={"id": "a1", "side": "ASK", "price": 101100.0},
        state="LONG_READY",
        reason_codes=["ASK_CONSUMED_BY_TRADES", "FIXTURE"],
        metrics={"fixture": True},
    )
    path = append_session_event(rec, base=tmp_path)
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    loaded = json.loads(lines[0])
    assert loaded["schema"] == "wall_decision_shadow_v1"
    assert loaded["session_id"] == "wd1_BTCUSDT_1"
    assert loaded["rule_version"] == RULE_VERSION

    rec2 = new_session_record(
        session_id="wd1_BTCUSDT_2",
        symbol="BTCUSDT",
        breakpoint=101060.0,
        target_wall=None,
        state="DATA_GAP",
        reason_codes=["DATA_GAP"],
    )
    append_session_event(rec2, base=tmp_path)
    recent = list_recent_sessions(limit=10, base=tmp_path)
    assert len(recent) == 2
    assert recent[0]["session_id"] == "wd1_BTCUSDT_2"


def test_api_router_exposes_config_and_shadow_routes():
    from wall_decision_v1.api import build_router

    async def _auth():
        return {"username": "test"}

    router = build_router(require_auth=_auth)
    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/api/wall-decision/v1/config" in paths
    assert "/api/wall-decision/v1/shadow" in paths
