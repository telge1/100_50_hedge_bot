"""Rule evaluation tables. OPEN excluded from winrate and PnL. Outcomes never rewritten."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import FEE_PP, RULE_IDS
from .warnings import RULE_DESCRIPTIONS
from research.stoch_fade_filter_tests.zec_5m_exhaustion.metrics import pnl_metrics


def kept_frame(decisions: pd.DataFrame, rule_id: str) -> pd.DataFrame:
    if rule_id == "R0":
        return decisions
    return decisions.loc[decisions[f"block_{rule_id}"] != True].copy()


def blocked_frame(decisions: pd.DataFrame, rule_id: str) -> pd.DataFrame:
    if rule_id == "R0":
        return decisions.iloc[0:0].copy()
    return decisions.loc[decisions[f"block_{rule_id}"] == True].copy()


def rule_row(decisions: pd.DataFrame, rule_id: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    kept = kept_frame(decisions, rule_id)
    blocked = blocked_frame(decisions, rule_id)
    base = pnl_metrics(decisions, variant="R0")
    after = pnl_metrics(kept, variant=rule_id)
    row = {
        "rule_id": rule_id,
        "rule": RULE_DESCRIPTIONS[rule_id],
        "n_baseline": int(len(decisions)),
        "n_blocked": int(len(blocked)),
        "n_kept": int(len(kept)),
        "block_rate": (len(blocked) / len(decisions)) if len(decisions) else None,
        "blocked_wins": int((blocked["outcome"] == "WIN").sum()),
        "blocked_losses": int((blocked["outcome"] == "LOSS").sum()),
        "blocked_open": int((blocked["outcome"] == "OPEN").sum()),
        "kept_wins": after["wins"],
        "kept_losses": after["losses"],
        "kept_open": after["open"],
        "winrate_before": base["winrate"],
        "winrate_after": after["winrate"],
        "gross_sum_before": base["gross_sum"],
        "gross_sum_after": after["gross_sum"],
        "gross_pf_before": base["gross_pf"],
        "gross_pf_after": after["gross_pf"],
        "fees_before": base["fees_total_pp"],
        "fees_after": after["fees_total_pp"],
        "net_sum_before": base["net_sum"],
        "net_sum_after": after["net_sum"],
        "net_sum_delta": after["net_sum"] - base["net_sum"],
        "net_pf_before": base["net_pf"],
        "net_pf_after": after["net_pf"],
        "net_mean_before": base["net_mean"],
        "net_mean_after": after["net_mean"],
        "longest_loss_streak_before": base["longest_loss_streak"],
        "longest_loss_streak_after": after["longest_loss_streak"],
        "fee_pp": FEE_PP,
    }
    if extra:
        row.update(extra)
    return row


def all_rules_table(decisions: pd.DataFrame, **extra: Any) -> pd.DataFrame:
    return pd.DataFrame([rule_row(decisions, rid, extra=extra or None) for rid in RULE_IDS])


def grouped_rules(decisions: pd.DataFrame, key: str) -> pd.DataFrame:
    rows = []
    for name, part in decisions.groupby(key, dropna=False):
        rows.append(all_rules_table(part, **{key: str(name)}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def score_outcomes(decisions: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for score, part in decisions.groupby("warning_score_true"):
        closed = part.loc[part["outcome"].isin(["WIN", "LOSS"])]
        n = int(len(closed))
        losses = int((closed["outcome"] == "LOSS").sum())
        rows.append(
            {
                "warning_score_true": int(score),
                "n_trades": int(len(part)),
                "n_closed": n,
                "n_win": int((part["outcome"] == "WIN").sum()),
                "n_loss": int((part["outcome"] == "LOSS").sum()),
                "n_open": int((part["outcome"] == "OPEN").sum()),
                "n_any_missing": int(part["warning_any_missing"].sum()),
                "loss_rate": (losses / n) if n else None,
                "net_sum": float(pd.to_numeric(closed["pnl_pct_net"], errors="coerce").sum()) if n else 0.0,
                "median_hold_seconds": float(pd.to_numeric(closed["hold_seconds"], errors="coerce").median()) if n else None,
            }
        )
    return pd.DataFrame(rows).sort_values("warning_score_true")


def recovery_table(decisions: pd.DataFrame, paths: pd.DataFrame) -> pd.DataFrame:
    merged = decisions.merge(paths, on="signal_id", how="left", suffixes=("", "_path"))
    entry = pd.to_datetime(merged["entry_time"], utc=True)
    exit_t = pd.to_datetime(merged["exit_time"], utc=True)
    rows = []
    for hz, td in (("4h", "4h"), ("6h", "6h"), ("12h", "12h"), ("24h", "24h")):
        sl_early = (merged["exit_reason"] == "SL") & exit_t.notna() & (exit_t <= entry + pd.to_timedelta(td))
        ok = merged.get(f"{hz}_status", pd.Series(index=merged.index, dtype=object)) == "OK"
        aligned = pd.to_numeric(merged.get(f"{hz}_aligned_return_pct"), errors="coerce")
        recov = sl_early & ok & (aligned > 0)
        n_sl = int(sl_early.sum())
        rows.append(
            {
                "horizon": hz,
                "n_sl_before_horizon": n_sl,
                "n_sl_then_aligned": int(recov.sum()),
                "share_recover": (float(recov.sum()) / n_sl) if n_sl else None,
            }
        )
    return pd.DataFrame(rows)


def path_cohort_summary(decisions: pd.DataFrame, paths: pd.DataFrame) -> pd.DataFrame:
    merged = decisions.merge(paths, on="signal_id", how="left", suffixes=("", "_path"))
    rows = []

    def add(cohort: str, part: pd.DataFrame) -> None:
        for hz in ("15m", "30m", "1h", "2h", "4h", "6h", "12h", "24h"):
            ok = part.loc[part.get(f"{hz}_status", pd.Series(dtype=object)) == "OK"] if f"{hz}_status" in part.columns else part.iloc[0:0]
            aligned = pd.to_numeric(ok.get(f"{hz}_aligned_return_pct"), errors="coerce") if len(ok) else pd.Series(dtype=float)
            hold = pd.to_numeric(part.get("hold_seconds"), errors="coerce")
            rows.append(
                {
                    "cohort": cohort,
                    "horizon": hz,
                    "n": int(len(part)),
                    "n_ok": int(len(ok)),
                    "share_in_direction": float((ok[f"{hz}_in_direction"] == True).mean()) if len(ok) and f"{hz}_in_direction" in ok else None,
                    "median_aligned": float(aligned.median()) if len(aligned) and aligned.notna().any() else None,
                    "mean_aligned": float(aligned.mean()) if len(aligned) and aligned.notna().any() else None,
                    "share_still_open": float(ok[f"{hz}_still_open"].astype(bool).mean()) if len(ok) and f"{hz}_still_open" in ok else None,
                    "median_hold_seconds": float(hold.median()) if hold.notna().any() else None,
                }
            )

    add("ALL", merged)
    for score, part in merged.groupby("warning_score_true"):
        add(f"score_{int(score)}", part)
    add("score_ge2", merged.loc[merged["warning_score_true"] >= 2])
    add("score_ge3", merged.loc[merged["warning_score_true"] >= 3])
    for outcome, part in merged.groupby("outcome"):
        add(f"outcome_{outcome}", part)
    for direction, part in merged.groupby("direction"):
        add(f"side_{direction}", part)
    for tf, part in merged.groupby("timeframe"):
        add(f"tf_{tf}", part)
    for rid in RULE_IDS:
        if rid == "R0":
            continue
        add(f"{rid}_BLOCKED", merged.loc[merged[f"block_{rid}"] == True])
        add(f"{rid}_KEPT", merged.loc[merged[f"block_{rid}"] != True])
    return pd.DataFrame(rows)


def fast_slow_stats(decisions: pd.DataFrame, rule_id: str) -> dict[str, Any]:
    wins = decisions.loc[decisions["outcome"] == "WIN"].copy()
    losses = decisions.loc[decisions["outcome"] == "LOSS"].copy()
    if rule_id == "R0":
        wins["blocked"] = False
        losses["blocked"] = False
    else:
        wins["blocked"] = wins[f"block_{rule_id}"] == True
        losses["blocked"] = losses[f"block_{rule_id}"] == True
    w_hold = pd.to_numeric(wins["hold_seconds"], errors="coerce")
    l_hold = pd.to_numeric(losses["hold_seconds"], errors="coerce")
    le15 = w_hold <= 15 * 60
    long_loss = l_hold >= 4 * 3600
    return {
        "rule_id": rule_id,
        "share_blocked_wins": float(wins["blocked"].mean()) if len(wins) else None,
        "share_blocked_fast_wins_le15m": float(wins.loc[le15, "blocked"].mean()) if int(le15.sum()) else None,
        "n_fast_wins_le15m": int(le15.sum()),
        "median_hold_blocked_wins": float(w_hold.loc[wins["blocked"]].median()) if bool(wins["blocked"].any()) else None,
        "median_hold_kept_wins": float(w_hold.loc[~wins["blocked"]].median()) if bool((~wins["blocked"]).any()) else None,
        "share_blocked_losses": float(losses["blocked"].mean()) if len(losses) else None,
        "share_blocked_long_losses_ge4h": float(losses.loc[long_loss, "blocked"].mean()) if int(long_loss.sum()) else None,
        "n_long_losses_ge4h": int(long_loss.sum()),
        "median_hold_blocked_losses": float(l_hold.loc[losses["blocked"]].median()) if bool(losses["blocked"].any()) else None,
        "median_hold_kept_losses": float(l_hold.loc[~losses["blocked"]].median()) if bool((~losses["blocked"]).any()) else None,
    }
