"""Orchestrate H1 liquidation exhaustion event audit (read-only)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from research.regime_scanner.liquidation_exhaustion.bursts import (
    collect_raw_burst_buckets,
    detect_bursts,
    oi_filter,
    price_filter,
)
from research.regime_scanner.liquidation_exhaustion.clustering import cluster_bursts, clusters_to_rows
from research.regime_scanner.liquidation_exhaustion.config import (
    BURST_VARIANTS,
    LEConfig,
    OI_VARIANTS,
    PRICE_VARIANTS,
    RECLAIM_VARIANTS,
    RECLAIM_WINDOWS,
    default_config,
    variant_id,
)
from research.regime_scanner.liquidation_exhaustion.controls import sample_controls
from research.regime_scanner.liquidation_exhaustion.features import enrich_features
from research.regime_scanner.liquidation_exhaustion.loader import load_joined_5m
from research.regime_scanner.liquidation_exhaustion.outcomes import (
    compute_forward_outcomes,
    diagnostic_exits,
)
from research.regime_scanner.liquidation_exhaustion.reclaim import check_reclaim

logger = logging.getLogger(__name__)


def attach_btc_context(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    if "BTCUSDT" not in frames:
        return frames
    btc = frames["BTCUSDT"][["bucket_start", "ret_15m_pct", "ret_1h_pct", "oi_chg_5m", "B1_long", "B1_short"]].copy()
    btc = btc.rename(
        columns={
            "ret_15m_pct": "btc_ret_15m_pct",
            "ret_1h_pct": "btc_ret_1h_pct",
            "oi_chg_5m": "btc_oi_chg_5m",
            "B1_long": "btc_B1_long",
            "B1_short": "btc_B1_short",
        }
    )
    out = {}
    for sym, df in frames.items():
        if sym == "BTCUSDT":
            out[sym] = df
            continue
        m = df.merge(btc, on="bucket_start", how="left")
        out[sym] = m
    return out


def run_symbol_pipeline(df: pd.DataFrame, cfg: LEConfig) -> dict[str, Any]:
    """Features → bursts → clusters → reclaim → outcomes for one symbol frame."""
    df = enrich_features(df)
    df = detect_bursts(df)
    raw = collect_raw_burst_buckets(df)

    clusters_all = []
    events = []
    reclaims = []
    outcomes = []

    for burst in BURST_VARIANTS:
        for side in ("long", "short"):
            clusters = cluster_bursts(df, burst=burst, side=side, cooldown=cfg.cooldown_bars)
            clusters_all.extend(clusters_to_rows(clusters, df))
            for cl in clusters:
                row = df.iloc[cl.anchor_i]
                for p in PRICE_VARIANTS:
                    if not price_filter(row, side, p):
                        continue
                    for o in OI_VARIANTS:
                        if not oi_filter(row, o):
                            continue
                        ev = {
                            "symbol": cl.symbol,
                            "side": side,
                            "burst": burst,
                            "price": p,
                            "oi": o,
                            "anchor_index": cl.anchor_i,
                            "anchor_bucket": str(df["bucket_start"].iloc[cl.anchor_i]),
                            "anchor_liq_usd": cl.anchor_liq,
                            "sequence_id": cl.sequence_id,
                            "variant_burst_only": f"{burst}x{p}x{o}",
                        }
                        events.append(ev)

                        # burst-only outcome at next open after anchor
                        fill_i = cl.anchor_i + 1
                        if fill_i < len(df) and int(df["sequence_id"].iloc[fill_i]) == cl.sequence_id:
                            entry = float(df["open"].iloc[fill_i])
                            fo = compute_forward_outcomes(df, fill_i=fill_i, entry=entry, side=side)
                            outcomes.append(
                                {
                                    **ev,
                                    "entry_mode": "burst_next_open",
                                    "fill_i": fill_i,
                                    "fill_bucket": str(df["bucket_start"].iloc[fill_i]),
                                    "fill_price": entry,
                                    **fo,
                                }
                            )

                        for r in RECLAIM_VARIANTS:
                            for w in RECLAIM_WINDOWS:
                                rc = check_reclaim(
                                    df, anchor_i=cl.anchor_i, side=side, variant=r, window=w
                                )
                                if rc is None:
                                    continue
                                vid = variant_id(burst, p, o, r, w)
                                rec = {
                                    **ev,
                                    **rc,
                                    "variant_id": vid,
                                    "reclaim": r,
                                }
                                reclaims.append(rec)
                                fo = compute_forward_outcomes(
                                    df,
                                    fill_i=int(rc["fill_i"]),
                                    entry=float(rc["fill_price"]),
                                    side=side,
                                )
                                exits = diagnostic_exits(
                                    df,
                                    fill_i=int(rc["fill_i"]),
                                    entry=float(rc["fill_price"]),
                                    side=side,
                                )
                                outcomes.append(
                                    {
                                        **rec,
                                        "entry_mode": "reclaim_next_open",
                                        **fo,
                                        "n_exit_evals": len(exits),
                                    }
                                )

    controls = sample_controls(df, events=events)
    return {
        "frame": df,
        "raw_bursts": raw,
        "clusters": clusters_all,
        "events": events,
        "reclaims": reclaims,
        "outcomes": outcomes,
        "controls": controls,
    }


def run_audit(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    import_version: str,
    cfg: LEConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or default_config()
    joined = load_joined_5m(
        symbols=symbols, start=start, end=end, import_version=import_version
    )
    coverage = {
        "joined_rows": int(len(joined)),
        "symbols": symbols,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "import_version": import_version,
    }
    if joined.empty:
        return {
            "coverage": coverage,
            "joined": joined,
            "raw_bursts": [],
            "clusters": [],
            "events": [],
            "reclaims": [],
            "outcomes": [],
            "controls": [],
            "by_symbol": {},
        }

    # per-symbol coverage
    cov_rows = []
    for sym, g in joined.groupby("symbol"):
        cov_rows.append(
            {
                "symbol": sym,
                "joined_rows": len(g),
                "min_bucket": str(g["bucket_start"].min()),
                "max_bucket": str(g["bucket_start"].max()),
                "sequences": int(g["sequence_id"].nunique()),
            }
        )

    frames = {sym: g.reset_index(drop=True) for sym, g in joined.groupby("symbol", sort=True)}
    # enrich+burst first pass for BTC context
    enriched = {sym: detect_bursts(enrich_features(f)) for sym, f in frames.items()}
    enriched = attach_btc_context(enriched)

    by_symbol = {}
    all_raw, all_cl, all_ev, all_rc, all_out, all_ctl = [], [], [], [], [], []
    for sym, df in enriched.items():
        # re-run pipeline on already enriched frame (skip double enrich)
        # detect_bursts already applied; collect from df
        raw = collect_raw_burst_buckets(df)
        clusters_all = []
        events = []
        reclaims = []
        outcomes = []
        for burst in BURST_VARIANTS:
            for side in ("long", "short"):
                clusters = cluster_bursts(df, burst=burst, side=side, cooldown=cfg.cooldown_bars)
                clusters_all.extend(clusters_to_rows(clusters, df))
                for cl in clusters:
                    row = df.iloc[cl.anchor_i]
                    for p in PRICE_VARIANTS:
                        if not price_filter(row, side, p):
                            continue
                        for o in OI_VARIANTS:
                            if not oi_filter(row, o):
                                continue
                            ev = {
                                "symbol": cl.symbol,
                                "side": side,
                                "burst": burst,
                                "price": p,
                                "oi": o,
                                "anchor_index": cl.anchor_i,
                                "anchor_bucket": str(df["bucket_start"].iloc[cl.anchor_i]),
                                "anchor_liq_usd": cl.anchor_liq,
                                "sequence_id": cl.sequence_id,
                                "variant_burst_only": f"{burst}x{p}x{o}",
                                "btc_ret_15m_pct": row.get("btc_ret_15m_pct"),
                                "btc_ret_1h_pct": row.get("btc_ret_1h_pct"),
                            }
                            events.append(ev)
                            fill_i = cl.anchor_i + 1
                            if fill_i < len(df) and int(df["sequence_id"].iloc[fill_i]) == cl.sequence_id:
                                entry = float(df["open"].iloc[fill_i])
                                fo = compute_forward_outcomes(df, fill_i=fill_i, entry=entry, side=side)
                                outcomes.append(
                                    {
                                        **ev,
                                        "entry_mode": "burst_next_open",
                                        "fill_i": fill_i,
                                        "fill_bucket": str(df["bucket_start"].iloc[fill_i]),
                                        "fill_price": entry,
                                        **fo,
                                    }
                                )
                            for r in RECLAIM_VARIANTS:
                                for w in RECLAIM_WINDOWS:
                                    rc = check_reclaim(
                                        df, anchor_i=cl.anchor_i, side=side, variant=r, window=w
                                    )
                                    if rc is None:
                                        continue
                                    rec = {
                                        **ev,
                                        **rc,
                                        "variant_id": variant_id(burst, p, o, r, w),
                                        "reclaim": r,
                                    }
                                    reclaims.append(rec)
                                    fo = compute_forward_outcomes(
                                        df,
                                        fill_i=int(rc["fill_i"]),
                                        entry=float(rc["fill_price"]),
                                        side=side,
                                    )
                                    outcomes.append(
                                        {
                                            **rec,
                                            "entry_mode": "reclaim_next_open",
                                            **fo,
                                        }
                                    )
        controls = sample_controls(df, events=events)
        by_symbol[sym] = {
            "n_raw": len(raw),
            "n_clusters": len(clusters_all),
            "n_events": len(events),
            "n_reclaims": len(reclaims),
            "n_outcomes": len(outcomes),
        }
        all_raw.extend(raw)
        all_cl.extend(clusters_all)
        all_ev.extend(events)
        all_rc.extend(reclaims)
        all_out.extend(outcomes)
        all_ctl.extend(controls)

    coverage["by_symbol"] = cov_rows
    return {
        "coverage": coverage,
        "joined": joined,
        "raw_bursts": all_raw,
        "clusters": all_cl,
        "events": all_ev,
        "reclaims": all_rc,
        "outcomes": all_out,
        "controls": all_ctl,
        "by_symbol": by_symbol,
        "config_hash": cfg.config_hash(),
    }
