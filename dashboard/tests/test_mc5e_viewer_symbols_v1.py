"""MC5E — multicoin 5m PD1 viewer symbol allowlist includes ZEC/QNT pipeline lines."""

from __future__ import annotations

from research_charts.multicoin_pd1_scope_v1 import (
    MC5E_5M_PD1_VIEWER_SYMBOLS,
    MC7A_QNT_FULL_LINE,
    MC7A_ZEC_FULL_LINE,
    resolve_multicoin_pd1_scope,
)


def test_mc5e_allowlist_includes_zec_qnt():
    assert "ZECUSDT" in MC5E_5M_PD1_VIEWER_SYMBOLS
    assert "QNTUSDT" in MC5E_5M_PD1_VIEWER_SYMBOLS


def test_zec_qnt_pipeline_lines():
    zec = resolve_multicoin_pd1_scope("ZECUSDT")
    qnt = resolve_multicoin_pd1_scope("QNTUSDT")
    assert zec.pipeline_line_id == MC7A_ZEC_FULL_LINE
    assert qnt.pipeline_line_id == MC7A_QNT_FULL_LINE