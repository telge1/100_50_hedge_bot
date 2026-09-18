"""mp_qdh_30event_case_control_v2 — hardened canonical 30-event case-control.

Reuses the frozen v1 event universe hashes. Fixes active-path gaps from the
masterplan audit without retuning thresholds or inventing new entry rules.
"""

from __future__ import annotations

PACKAGE_NAME = "mp_qdh_30event_case_control_v2"
STUDY_ID = "MP_QDH_30EVENT_CASE_CONTROL_V2"
CONTRACT_VERSION = "2.0.0"
SCHEMA_VERSION = "mp_qdh_30event_case_control_v2"

TICK_SIZE = 0.1
BAND_TICKS = 5
PRE_TOUCH_S = 120.0
FORENSIC_TAIL_S = 300.0
CAUSAL_SNAPSHOT_S = (5, 15, 30, 60, 120)

ALLOW_CLICKHOUSE_WRITES = False
SILVER_DATABASE = "research_full_ob_silver_v1_3"
SYMBOL = "BTCUSDT"

V1_RUN_REL = "obfull_research_engine/runs/mp_qdh_30event_case_control_v1_20260917"
BATCH_RUN_REL = "obfull_research_engine/runs/mp_edge_event_batch_v1_20260916"

# Expected frozen hashes from v1 (must match or EVENT_UNIVERSE_MISMATCH)
EXPECTED_EVENT_LIST_SHA256 = "a3b05d6b10ef658c552d87d6977bb411e3e662453cde58fc4751a3bd1b5f50cb"
EXPECTED_PAIR_LIST_SHA256 = "8fd3ab64b809338cc42e76280ae25cd7dce41c627152adc1633fa9a808acdac9"
EXPECTED_N_PAIRS = 15

ABSORPTION_RATIO_STATUS = "NOT_CALIBRATED"
VACUUM_SCORE_STATUS = "NOT_CALIBRATED"

PHASE0_CONTRACT = {
    "universe": "Reuse v1 frozen_event_universe + matched_pairs; verify SHA256",
    "coverage_gate": "Block event on zero public trades, cross_epoch>0, seq_gaps>0, no silver",
    "persistence": "update_aggressor → update_qdh(persistence_ratio=state.persistence_ratio)",
    "fill_pull": "AttributedFill_capped=min(raw_fill, book_decrease); Pull=max(book_dec-fill_capped,0); UNKNOWN for LOW+no-fill decrease",
    "qdh_missing": "QUEUE_EXHAUSTED/QUEUE_UNKNOWN → explicit NOT_AVAILABLE reason; AUC / decision_horizon_s",
    "impact_efficiency": "progress_bps / (hit_notional_mio + eps) via price_response.update_price_response",
    "depth": "same_side_2bps / pre_touch_120s_median_baseline - 1; raw kept separately",
    "absorption_vacuum": "NOT_CALIBRATED formal placeholders only",
}
