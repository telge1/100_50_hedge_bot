from __future__ import annotations

import json
from pathlib import Path

from ema_pool_trend_flip_v1.research_feed import paginated_signals_page
from ema_pool_trend_flip_v1.schema import STRATEGY_ID

DASHBOARD = Path(__file__).resolve().parents[2]
PLANNER = "c6c960a82e9a0c538dbe24b03f481893e722072f"


def _write_run(tmp_path: Path, trades: list[dict]) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    manifest = {
        "run_id": "t1",
        "strategy_id": STRATEGY_ID,
        "complete": True,
        "productive": True,
        "test_fixture_only": False,
        "pool_candle_source": "clickhouse",
        "clickhouse": {
            "pool_candle_source": "clickhouse",
            "database": "signal_generator",
            "table": "candles_1m",
            "exchange": "bybit",
            "interval": "1m",
            "final": True,
            "is_closed": 1,
        },
        "planner": {"pin_ok": True, "commit": PLANNER},
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run / "trades.jsonl").write_text(
        "\n".join(json.dumps(row) for row in trades) + "\n",
        encoding="utf-8",
    )
    (run / "blocked_signals.jsonl").write_text("", encoding="utf-8")
    (run / "ignored_duplicates.jsonl").write_text("", encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({"STATIC": {"wins": 0}}), encoding="utf-8")
    reg = {
        "strategy_id": STRATEGY_ID,
        "research_only": True,
        "live_trading": False,
        "artifact_dir": str(run),
        "planner_version": PLANNER,
        "symbol": "ACEUSDT",
        "window_start": "2026-08-12T15:03:43Z",
        "window_end": "2026-08-14T15:03:43Z",
    }
    reg_path = tmp_path / "reg.json"
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    return reg_path


def _static_trade(signal_id: str, symbol: str, ts: str, direction: str = "LONG") -> dict:
    return {
        "signal_id": signal_id,
        "symbol": symbol,
        "variant": "EMA_POOL_TREND_FLIP_V1_STATIC",
        "decision": "ALIGNED",
        "executed_direction": direction,
        "original_direction": direction,
        "signal_time": ts,
        "entry_time": ts,
        "entry_price": 1.0,
        "outcome": "OPEN",
        "signal_timeframe": "15m",
    }


def test_nav_dropdown_daten_signale():
    nav = (DASHBOARD / "templates" / "partials" / "nav.html").read_text(encoding="utf-8")
    assert "📉 Daten & Signale" in nav
    assert "Datenhub" in nav
    assert "Backtester" in nav
    assert 'href="/stoch-signale"' in nav
    assert 'href="/backtester"' in nav
    assert "Ema-Signal" not in nav
    assert nav.count("Daten & Signale") == 2
    assert 'aria-label="Daten & Signale"' in nav
    assert "📉 Stoch-Signale" not in nav


def test_datenhub_template_renamed():
    html = (DASHBOARD / "templates" / "stoch_signale.html").read_text(encoding="utf-8")
    assert "set nav_active = 'daten-signale-datenhub'" in html
    assert "Datenhub" in html
    assert "<title>Datenhub" in html


def test_ema_signal_template_renders():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(DASHBOARD / "templates")))
    html = env.get_template("ema_signale.html").render(
        user={"username": "tester"},
        banner={"title": "V2 STRATEGY BACKTESTER"},
        window_label="SMOKE / STATE_WARMUP_NOT_PROVEN",
        page_size=50,
        signal_page=0,
    )
    assert "V2 Strategy Backtester" in html
    assert "<title>V2 Strategy Backtester</title>" in html
    assert "SMOKE / STATE_WARMUP_NOT_PROVEN" in html
    assert "ema_signale_v2_chart.js" in html
    assert "v2KpiTrades" in html


def test_ema_signal_template_contract():
    html = (DASHBOARD / "templates" / "ema_signale.html").read_text(encoding="utf-8")
    js = (DASHBOARD / "static" / "js" / "ema_signale.js").read_text(encoding="utf-8")
    app = (DASHBOARD / "app.py").read_text(encoding="utf-8")
    assert "set nav_active = 'daten-signale-backtester'" in html
    assert "Decision-Close (UTC)" in html
    assert ">Symbol<" in html
    assert "v2-backtest" in app
    assert 'id="v2FilterForm"' in html
    assert "emaPagePrev" in html
    assert "emaPageNext" in js
    assert '@app.get("/backtester"' in app
    assert '@app.get("/ema-signale"' in app
    assert "status_code=308" in app
    assert "ema_signale_legacy_redirect" in app
    assert '@app.get("/daten-signale"' in app
    assert "paginated_live_signals" not in app.split("def backtester_page")[1].split("@app.get")[0]


def test_pagination_newest_first(tmp_path, monkeypatch):
    trades = [
        _static_trade("a", "ACEUSDT", "2026-08-12T15:00:00Z"),
        _static_trade("b", "ACEUSDT", "2026-08-12T17:00:00Z"),
        _static_trade("c", "ACEUSDT", "2026-08-12T16:00:00Z"),
    ]
    reg = _write_run(tmp_path, trades)
    monkeypatch.setenv("EMA_POOL_TREND_FLIP_RESEARCH_REGISTRY", str(reg))
    first = paginated_signals_page(page=0, page_size=1)
    assert first["feed_ready"] is True
    assert first["pagination"]["total_filtered"] == 3
    assert first["pagination"]["has_next"] is True
    assert first["pagination"]["has_prev"] is False
    assert first["signals"][0]["signal_id"] == "b"
    assert first["signals"][0]["signal_time_label"]
    second = paginated_signals_page(page=1, page_size=1)
    assert second["signals"][0]["signal_id"] == "c"
    assert second["pagination"]["has_prev"] is True
    third = paginated_signals_page(page=2, page_size=1)
    assert third["signals"][0]["signal_id"] == "a"
    assert third["pagination"]["has_next"] is False


def test_symbol_and_time_filters(tmp_path, monkeypatch):
    trades = [
        _static_trade("a", "ACEUSDT", "2026-08-12T15:00:00Z", "LONG"),
        _static_trade("b", "ETHUSDT", "2026-08-12T17:00:00Z", "SHORT"),
        _static_trade("c", "ACEUSDT", "2026-08-12T18:00:00Z", "SHORT"),
    ]
    reg = _write_run(tmp_path, trades)
    monkeypatch.setenv("EMA_POOL_TREND_FLIP_RESEARCH_REGISTRY", str(reg))
    by_sym = paginated_signals_page(symbol="ACEUSDT", page_size=50)
    assert {r["signal_id"] for r in by_sym["signals"]} == {"a", "c"}
    assert by_sym["symbols"] == ["ACEUSDT", "ETHUSDT"]
    by_dir = paginated_signals_page(direction="SHORT", page_size=50)
    assert {r["signal_id"] for r in by_dir["signals"]} == {"b", "c"}
    by_time = paginated_signals_page(start_time="2026-08-12T16:30", page_size=50)
    assert {r["signal_id"] for r in by_time["signals"]} == {"b", "c"}
