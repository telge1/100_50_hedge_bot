#!/usr/bin/env python3
"""Run Wave-Fade shadow signal pipeline (history replay / catch-up).

Shadow mode only — never places orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.processing_state import ProcessingStateRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import SignalRepository  # noqa: E402
from signal_generator.pipeline.audit import htf_bar_matches_manual, signal_fingerprint  # noqa: E402
from signal_generator.pipeline.processor import (  # noqa: E402
    ShadowPipelineConfig,
    WaveFadeShadowPipeline,
)
from signal_generator.pipeline.versions import (  # noqa: E402
    EDGES_VERSION,
    GENERATOR_VERSION,
    GLOBAL_FROZEN_TIER_A,
    STRATEGY_VERSION,
)
from signal_generator.strategy.wave_fade.adapter import bars_to_ohlcv_df  # noqa: E402
from signal_generator.strategy.wave_fade.edges import load_frozen_eff_edges  # noqa: E402
from signal_generator.strategy.wave_fade.signals import (  # noqa: E402
    build_symbol_signals,
    build_waves_from_ohlcv,
)
from signal_generator.timeframes import (  # noqa: E402
    STRATEGY_TIMEFRAMES,
    aggregate_1m_to_timeframe,
    bars_from_mappings,
    bucket_start,
    ensure_utc,
)


def _parse_ts(s: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(s.replace("Z", "+00:00")))


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def pd_to_utc(x):
    import pandas as pd

    t = pd.Timestamp(x)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def _fps(signals: SignalRepository, symbols: list[str], start: datetime, end: datetime) -> set:
    out = set()
    for sym in symbols:
        for r in signals.get_signals(sym, start, end, time_field="candle_close_time"):
            if (
                r.get("generator_version") == GENERATOR_VERSION
                and r.get("strategy_version") == STRATEGY_VERSION
            ):
                out.add(signal_fingerprint(r))
    return out


def _cleanup(signals: SignalRepository, state: ProcessingStateRepository) -> None:
    signals.delete_by_generator_version(GENERATOR_VERSION)
    state.delete_for_strategy(STRATEGY_VERSION)
    time.sleep(2.0)  # allow lightweight mutations to apply


def run_parity(client, symbols, start, end, lookback) -> tuple[list[dict], str]:
    candles = CandleRepository(client)
    signals = SignalRepository(client)
    edges = load_frozen_eff_edges()
    rows_out: list[dict] = []
    status = "PASS"
    for symbol in symbols:
        load_start = start - lookback
        raw = candles.get_candles(symbol, load_start, end)
        bars = bars_from_mappings(raw)
        as_of = min(end, max(ensure_utc(b.close_time) for b in bars)) if bars else end
        waves = {}
        for tf in STRATEGY_TIMEFRAMES:
            htf = aggregate_1m_to_timeframe(bars, tf, as_of=as_of, require_complete=True)
            waves[tf] = build_waves_from_ohlcv(
                bars_to_ohlcv_df(htf), symbol=symbol, timeframe=tf
            )
        core = build_symbol_signals(symbol, edges, waves)
        if not core.empty:
            conf = core["confirmation_available_at"].map(pd_to_utc)
            core = core[(conf >= start) & (conf < end)]

        db = [
            r
            for r in signals.get_signals(symbol, start, end, time_field="candle_close_time")
            if r.get("strategy_version") == STRATEGY_VERSION
            and r.get("generator_version") == GENERATOR_VERSION
        ]

        core_keys = set()
        if not core.empty:
            for _, r in core.iterrows():
                core_keys.add(
                    (
                        symbol,
                        str(r["signal_tf"]),
                        str(r["side"]),
                        pd_to_utc(r["confirmation_available_at"]).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        int(bool(r["is_tier_a"])),
                    )
                )
        db_keys = set()
        for r in db:
            db_keys.add(
                (
                    symbol,
                    str(r["timeframe"]),
                    str(r["direction"]),
                    ensure_utc(r["candle_close_time"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    int(r["tier_a"]),
                )
            )
        only_core = core_keys - db_keys
        only_db = db_keys - core_keys
        ok = not only_core and not only_db
        if not ok:
            status = "FAIL"
        rows_out.append(
            {
                "symbol": symbol,
                "core_count": len(core_keys),
                "db_count": len(db_keys),
                "only_core": len(only_core),
                "only_db": len(only_db),
                "ok": ok,
            }
        )
    return rows_out, status


def run_htf_audit(client, symbols, start, end) -> tuple[list[dict], str]:
    candles = CandleRepository(client)
    out = []
    status = "PASS"
    mid = start + (end - start) / 2
    for symbol in symbols[:1]:
        raw = candles.get_candles(symbol, start, end)
        for tf in STRATEGY_TIMEFRAMES:
            bo = bucket_start(mid, tf)
            res = htf_bar_matches_manual(raw, timeframe=tf, bucket_open=bo, as_of=end)
            out.append(
                {
                    "symbol": symbol,
                    "timeframe": tf,
                    "bucket_open": res.get("bucket_open"),
                    "ok": res["ok"],
                    "reason": res.get("reason", ""),
                }
            )
            if not res["ok"]:
                status = "FAIL"
    return out, status


def summarize_signals(client, symbols, start, end) -> list[dict]:
    signals = SignalRepository(client)
    rows = []
    for symbol in symbols:
        for tf in STRATEGY_TIMEFRAMES:
            got = [
                r
                for r in signals.get_signals(
                    symbol, start, end, timeframe=tf, time_field="candle_close_time"
                )
                if r.get("strategy_version") == STRATEGY_VERSION
                and r.get("generator_version") == GENERATOR_VERSION
            ]
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": tf,
                    "candidates": len(got),
                    "tier_a": sum(int(r["tier_a"]) for r in got),
                    "long": sum(1 for r in got if r["direction"] == "LONG"),
                    "short": sum(1 for r in got if r["direction"] == "SHORT"),
                }
            )
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", default="APTUSDT,DOGEUSDT,BTCUSDT")
    p.add_argument("--start", default="2026-08-01T00:00:00Z")
    p.add_argument("--end", default="2026-08-03T00:00:00Z")
    p.add_argument("--lookback-days", type=int, default=14)
    p.add_argument("--artifacts", default="results/wave_fade_shadow_signal_pipeline")
    p.add_argument("--cleanup-first", action="store_true", default=True)
    p.add_argument("--no-cleanup-first", action="store_false", dest="cleanup_first")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start = _parse_ts(args.start)
    end = _parse_ts(args.end)
    lookback = timedelta(days=args.lookback_days)
    mid = start + (end - start) / 2
    art = Path(args.artifacts)
    art.mkdir(parents=True, exist_ok=True)

    client = setup_clickhouse(settings=get_clickhouse_settings())
    try:
        candles = CandleRepository(client)
        signals = SignalRepository(client)
        state = ProcessingStateRepository(client)

        if args.cleanup_first:
            logging.info("cleanup prior shadow signals/state …")
            _cleanup(signals, state)

        cfg = ShadowPipelineConfig(
            symbols=symbols, start=start, end=end, lookback=lookback, shadow=True
        )
        logging.info("one-shot shadow run %s → %s", start, end)
        metrics = WaveFadeShadowPipeline(
            candles=candles, signals=signals, state=state, config=cfg
        ).run()
        oneshot = _fps(signals, symbols, start, end)

        # Idempotency re-run
        WaveFadeShadowPipeline(
            candles=candles, signals=signals, state=state, config=cfg
        ).run()
        after_rerun = _fps(signals, symbols, start, end)
        idem_ok = oneshot == after_rerun

        # Resume: clean, partial then continue
        logging.info("resume test: clean + partial to mid + continue")
        _cleanup(signals, state)
        WaveFadeShadowPipeline(
            candles=candles,
            signals=signals,
            state=state,
            config=ShadowPipelineConfig(
                symbols=symbols, start=start, end=mid, lookback=lookback
            ),
        ).run()
        WaveFadeShadowPipeline(
            candles=candles,
            signals=signals,
            state=state,
            config=ShadowPipelineConfig(
                symbols=symbols, start=start, end=end, lookback=lookback
            ),
        ).run()
        resume_fps = _fps(signals, symbols, start, end)
        resume_ok = resume_fps == oneshot

        # Restore oneshot set if resume overwrote (same IDs — should match)
        # Re-run full oneshot path already equivalent via resume_ok

        by_sym = summarize_signals(client, symbols, start, end)
        _write_csv(
            art / "signals_by_symbol_tf.csv",
            by_sym,
            ["symbol", "timeframe", "candidates", "tier_a", "long", "short"],
        )

        parity_rows, parity_status = run_parity(client, symbols, start, end, lookback)
        _write_csv(
            art / "parity_check.csv",
            parity_rows,
            ["symbol", "core_count", "db_count", "only_core", "only_db", "ok"],
        )

        htf_rows, htf_status = run_htf_audit(client, symbols, start, end)
        _write_csv(
            art / "htf_aggregation_check.csv",
            htf_rows,
            ["symbol", "timeframe", "bucket_open", "ok", "reason"],
        )

        resume_rows = [
            {
                "check": "rerun_idempotency",
                "before": str(len(oneshot)),
                "after": str(len(after_rerun)),
                "ok": idem_ok,
            },
            {
                "check": "resume_vs_oneshot",
                "before": str(len(oneshot)),
                "after": str(len(resume_fps)),
                "ok": resume_ok,
            },
        ]
        for st in state.list_for_strategy(STRATEGY_VERSION):
            if st.symbol in symbols:
                resume_rows.append(
                    {
                        "check": "watermark",
                        "before": f"{st.symbol}:{st.timeframe}",
                        "after": st.last_processed_available_at.isoformat(),
                        "ok": True,
                    }
                )
        _write_csv(art / "resume_check.csv", resume_rows, ["check", "before", "after", "ok"])

        meta = {
            "strategy_version": STRATEGY_VERSION,
            "generator_version": GENERATOR_VERSION,
            "edges_version": EDGES_VERSION,
            "global_frozen_tier_a": GLOBAL_FROZEN_TIER_A,
            "per_symbol_refit": False,
            "mode": "shadow",
            "symbols": symbols,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "lookback_days": args.lookback_days,
            "parity": parity_status,
            "htf_aggregation": htf_status,
            "idempotency": "PASS" if idem_ok else "FAIL",
            "resume": "PASS" if resume_ok else "FAIL",
            "metrics": metrics.as_dict(),
            "signals_summary": by_sym,
            "logical_signal_count": len(oneshot),
        }
        (art / "run_metadata.json").write_text(
            json.dumps(meta, indent=2, default=str), encoding="utf-8"
        )

        lines = [
            "# Wave-Fade Shadow Signal Pipeline",
            "",
            f"- Strategy: `{STRATEGY_VERSION}`",
            f"- Edges: `{EDGES_VERSION}` (GLOBAL_FROZEN_TIER_A=YES, per-symbol refit=NO)",
            f"- Window: `{start.isoformat()}` → `{end.isoformat()}`",
            f"- Symbols: {', '.join(symbols)}",
            f"- Parity: **{parity_status}**",
            f"- HTF aggregation: **{htf_status}**",
            f"- Idempotency: **{'PASS' if idem_ok else 'FAIL'}**",
            f"- Resume: **{'PASS' if resume_ok else 'FAIL'}**",
            "",
            "## Signals",
            "",
            "| Symbol | TF | Candidates | Tier-A | Long | Short |",
            "| ------ | -- | ---------: | -----: | ---: | ----: |",
        ]
        for r in by_sym:
            lines.append(
                f"| {r['symbol']} | {r['timeframe']} | {r['candidates']} | "
                f"{r['tier_a']} | {r['long']} | {r['short']} |"
            )
        (art / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        logging.info("artifacts → %s", art)

        if parity_status != "PASS":
            return 2
        if htf_status != "PASS":
            return 3
        if not idem_ok:
            return 4
        if not resume_ok:
            return 5
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
