"""MC4-L — pipeline_line_id must drive load_historical_pool_state_at_close."""

from __future__ import annotations

import pytest

from research_charts.historical_pool_state_v1 import (
    HistoricalPoolStateError,
    load_historical_pool_state_at_close,
)
from research_charts.multicoin_pd1_scope_v1 import (
    DOGE_PROTECTED_5M_PD1_LINE,
    MC3D_SOL_FULL_LINE,
)


def test_doge_default_without_pipeline_arg_unchanged():
  from research_charts.historical_pool_state_v1 import doge_5m_pd1_viewer_bounds

  b = doge_5m_pd1_viewer_bounds()
  dc = b["data_available_to"].replace(" ", "T")
  st = load_historical_pool_state_at_close(
      dc,
      pool_detail=1,
      timeframe="5m",
      symbol="DOGEUSDT",
      include_remaining=False,
  )
  assert st.pipeline_line_id == DOGE_PROTECTED_5M_PD1_LINE


def test_sol_requires_matching_pipeline():
  from research_charts.historical_pool_state_v1 import multicoin_5m_pd1_viewer_bounds

  b = multicoin_5m_pd1_viewer_bounds("SOLUSDT")
  dc = b["data_available_to"].replace(" ", "T")
  st = load_historical_pool_state_at_close(
      dc,
      pool_detail=1,
      timeframe="5m",
      symbol="SOLUSDT",
      cfg_hash=b["config_hash"],
      pipeline_line_id=MC3D_SOL_FULL_LINE,
      include_remaining=False,
  )
  assert st.pipeline_line_id == MC3D_SOL_FULL_LINE
  assert st.symbol == "SOLUSDT"
  assert st.active_members


def test_sol_rejects_doge_pipeline_line():
  from research_charts.historical_pool_state_v1 import multicoin_5m_pd1_viewer_bounds

  b = multicoin_5m_pd1_viewer_bounds("SOLUSDT")
  dc = b["data_available_to"].replace(" ", "T")
  with pytest.raises(HistoricalPoolStateError) as exc:
    load_historical_pool_state_at_close(
        dc,
        pool_detail=1,
        timeframe="5m",
        symbol="SOLUSDT",
        cfg_hash=b["config_hash"],
        pipeline_line_id=DOGE_PROTECTED_5M_PD1_LINE,
        include_remaining=False,
    )
  assert exc.value.code == "INVALID_SCOPE"
