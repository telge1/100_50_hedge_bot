"""Phase 2A.4 preflight — frozen liquidation_flow_facts_v1."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .liquidation_flow_contract import (
    BYBIT_ALL_LIQUIDATION_DOCS_URL,
    BYBIT_SIDE_MAPPING,
    EVENT_KEY_FIELDS,
    EVENT_KEY_FORMAT,
    EVENT_KEY_VERSION,
    FORBIDDEN_RESEARCH_INPUT_PATHS,
    INPUT_SOURCES,
    LIQUIDATION_FLOW_CONTRACT,
    SUPERSEDED_EXPLANATORY_AUDIT,
    UNITS,
    frozen_contract_schema,
)

ROOT = Path(__file__).resolve().parents[2]
RESEARCH = ROOT / "research" / "btc_ob_fight"
RUN_017 = ROOT / "results" / "btc_ob_fight_cases" / "20260831T190000Z" / "run_017"
EXPL_OUT = ROOT / "results" / "btc_ob_fight_explanatory_audit_20260831_1900_v1"


def _git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return "UNKNOWN"


def build_phase_2a4_preflight() -> dict[str, Any]:
    return {
        "preflight_version": "phase_2a4_liquidation_flow_preflight_v2_frozen",
        "contract_version": LIQUIDATION_FLOW_CONTRACT,
        "contract_frozen": True,
        "git_head_at_preflight": _git_head(),
        "frozen_schema": frozen_contract_schema(),
        "bybit_documentation_url": BYBIT_ALL_LIQUIDATION_DOCS_URL,
        "bybit_side_mapping": BYBIT_SIDE_MAPPING,
        "side_mapping_note": "S = position side (NOT taker aggressor)",
        "event_key_version": EVENT_KEY_VERSION,
        "event_key_fields": list(EVENT_KEY_FIELDS),
        "event_key_format": EVENT_KEY_FORMAT,
        "input_sources": INPUT_SOURCES,
        "units": UNITS,
        "attribution_windows_ms": [100, 250, 500, 1000],
        "forbidden_research_input_paths": list(FORBIDDEN_RESEARCH_INPUT_PATHS),
        "superseded_explanatory_audit": SUPERSEDED_EXPLANATORY_AUDIT,
        "canonical_pipeline_does_not_load": [
            "results/btc_ob_fight_explanatory_audit_20260831_1900_v1",
            "liquidation_trade_association_sensitivity.csv from explanatory audit",
        ],
        "integrated_module": "research/btc_ob_fight/liquidation_flow_facts.py",
        "integrated_contract_module": "research/btc_ob_fight/liquidation_flow_contract.py",
        "run_017_reference": {
            "path": str(RUN_017),
            "exists": RUN_017.is_dir(),
            "unchanged_by_this_phase": True,
        },
        "explanatory_audit_output": {
            "path": str(EXPL_OUT),
            "exists": EXPL_OUT.is_dir(),
            "superseded_marker": str(EXPL_OUT / "SUPERSEDED.json"),
        },
    }


def write_preflight(path: Path | None = None) -> Path:
    out = path or RESEARCH / "phase_2a4_liquidation_flow_preflight.json"
    payload = build_phase_2a4_preflight()
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out
