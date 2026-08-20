# Frozen Rare Confluence Watchlist

**Status date (UTC):** 2026-08-20  
**Branch:** `research/confirmed-orderbook-entries`  
**HEAD:** `8ac8e77826d4125b5f31f79cee62a93a9dffc06f`  
**Repo:** `orderbook_analyse`

## Purpose

Freeze two rare-confluence trigger candidates as a **research watchlist** (not live alerts, not a strategy).

| Candidate | Side | Status intent |
|---|---|---|
| `SHORT_RARE_IMB_DELTA_TPS_V1` | SHORT | Watchlist / hard-test only |
| `LONG_RARE_IMB_OFI_DELTA_V1` | LONG | Watchlist / hard-test only |

## Data window (primary)

- **Evaluation:** `2026-08-11T00:00:00Z` … `2026-08-17T23:59:59Z` (exclusive end `2026-08-18`)
- **Warmup for quantiles / baselines:** from `2026-08-10T00:00:00Z`
- **Universe:** 51 coins with full `ob200_v3` coverage in window

### Sources (read-only)

- `signal_generator.candles_1m`
- `orderbook_analysis.public_trades_canonical`
- `orderbook_analysis.orderbook_features_1s_v2` (`parser_version=ob200_v3`, `depth=200`)
- LLD: **context only**, never as an extra filter for these candidates
- OI / liquidations: **not used** (no clean overlap in primary window)

## Frozen definitions (exact)

Shared rules for both candidates:

- Quantile flags are **causal**: `shift(1).rolling(1440, min_periods=180).quantile(...)`
- Decision only at **minute close** of the signal minute
- **Entry** = open of the **next** 1m candle
- Cooldown: **60 minutes** per coin per candidate
- Universal gates (from discovery; not new filters):
  - `ob_ok`: `seconds >= 30` and `valid_seconds/seconds >= 0.95`
  - `spread_ok`: `spread_5m <= trailing p50` (or NaN allowed as true)

Feature construction (shared):

- `tps_ratio` = `trade_count / trailing_24h_median(trade_count)`
- `delta_5m` = sum of aggressive buy−sell size over 5 minutes
- `ofi_5m` = sum of OFI over 5 minutes
- `imb50_5m` = mean `imbalance_l50` over 5 minutes
- `spread_5m` = mean `spread_bps` over 5 minutes

### 1) `SHORT_RARE_IMB_DELTA_TPS_V1`

```text
SHORT
AND imb50_lo_top1   # imb50_5m <= trailing p01
AND delta_lo_top2   # delta_5m  <= trailing p02
AND tps_top1        # tps_ratio >= trailing p99
AND ob_ok AND spread_ok
```

Origin: rare confluence discovery best SHORT combo; prior 1h hard-test reproduced n=12 exactly, holdout Aug16–17 failed (0/4 hits).

### 2) `LONG_RARE_IMB_OFI_DELTA_V1`

```text
LONG
AND imb50_hi_top1   # imb50_5m >= trailing p99
AND ofi_hi_top2     # ofi_5m   >= trailing p98
AND delta_hi_top2   # delta_5m >= trailing p98
AND ob_ok AND spread_ok
```

Origin: rare confluence discovery best LONG combo (`imb50_hi_top1+ofi_hi_top2+delta_hi_top2`, ~3 signals/day in discovery).

## Why frozen

- Derived from **big-move precondition discovery**, not from inventing R≥5 / TPS3 activity spam
- Rare frequency target (order of ~1–5/day across 51 coins)
- Discovery showed better **MFE with MAE not worse** than matched controls for these combos
- Need identical reuse for 1h **and** 4h path tests without retuning

## Known risks

- **n is small** (tens of events, not hundreds)
- **SHORT holdout** (2026-08-16…17) already failed once in 1h hard-test
- Signal minute often already moved (`ret_1m` not quiet) → entry may be late
- Only **7 OB days** in clean primary window; 2-day holdout is weak
- Quantile extremes are regime-sensitive; more days required before any alert promotion

## Hard rules (no silent retuning)

1. **Do not change parameters** without creating a **new candidate name** (e.g. `…_V2`).
2. Later tests **must reuse these exact definitions** (flags, gates, entry, cooldown).
3. Adding LLD / OI / new thresholds = **new candidate**, not an edit of V1.
4. Watchlist ≠ live alert. Promotion requires longer holdout with `pass_core`.

## Evaluation contract (path after entry)

For each signal, measure **1h (60m)** and **4h (240m)** after entry open:

- SHORT: `MFE=(entry-low)/entry`, `MAE=(high-entry)/entry`, `Return=(entry-close)/entry`
- LONG:  `MFE=(high-entry)/entry`, `MAE=(entry-low)/entry`, `Return=(close-entry)/entry`

Hits: MFE thresholds 0.50% / 0.75% / 1.00% (plus 1.50% / 2.00% on 4h).

Matched controls: same coin, UTC hour, 24h vol bucket; no candidate signal in ±60m; same path metrics.

Splits: full 11–17, discovery 11–15, holdout 16–17, leave-one-day-out, leave-one-coin-out.

## Artifact paths

| Kind | Path |
|---|---|
| This doc | `docs/research/FROZEN_RARE_CONFLUENCE_WATCHLIST.md` |
| Watchlist 1h+4h run | `results/frozen_rare_confluence_watchlist/20260811_17/` |
| Prior SHORT 1h hard-test | `results/frozen_hard_tests/SHORT_RARE_IMB_DELTA_TPS_V1_20260811_17/` |
| Quiet-Entry V2 hard-test (PARTIAL) | `results/frozen_hard_tests/RARE_CONFLUENCE_QUIET_ENTRY_V2_20260811_17/` |
| Discovery source | `results/rare_confluence_discovery/20260811_17_51coins/` |
| Runner (research) | `research/frozen_rare_confluence_watchlist_1h4h.py` |

## Verdict labels (this freeze cycle)

- Per candidate: `…_WATCHLIST` or `…_REJECTED`
- Overall: `FROZEN_RARE_CONFLUENCE_WATCHLIST_CREATED` or `FROZEN_RARE_CONFLUENCE_REJECTED`


## Latest freeze-cycle result

- Run UTC: 2026-08-20T16:52:29Z
- Overall: **FROZEN_RARE_CONFLUENCE_REJECTED**
- SHORT: **SHORT_RARE_IMB_DELTA_TPS_V1_REJECTED**
- LONG: **LONG_RARE_IMB_OFI_DELTA_V1_REJECTED**
- Artifacts: `/home/telgenbuescher/projects/orderbook_analyse/results/frozen_rare_confluence_watchlist/20260811_17`

---

## Follow-up (does **not** change V1): Quiet-Entry V2

**Candidate:** `RARE_CONFLUENCE_QUIET_ENTRY_V2`  
**Status:** **`PARTIAL` / `CONTEXT_ONLY`** — **not watchlist, no alert, not confirmed**  
**Window:** same clean OB window only (`2026-08-11`…`2026-08-17`, 51 coins). No OI/Liq.  
**Rule:** V1 definitions unchanged; V2 = cooldowned V1 events **subset** with a-priori quiet gate only (no retuning, no extra filters).

| Variant | Gate | Result |
|---|---|---|
| **A (best)** | `abs(event_minute_return) ≤ 0.15%` | In-sample MAE↓ / ratio↑; **holdout fail** |
| B | `≤ 0.10%` | Similar direction, smaller n |
| C | `≤ 2 × trailing_24h median |ret_1m|` | Too aggressive → rejected for promotion |

**Variant A headline (1h):** combined n=18 (of 33 V1); Hit075≈38.9%; ØMAE≈0.28% (vs V1 combined ratio 1.56 → V2 ratio **2.50**); holdout holds = **False**.

**Reuse:** Quiet-Entry remains a **timing / risk context** (“signal minute already moved → entry likely late”). Any parameter change requires a **new name** (`…_V3`). Do not silently edit V1.

**Artifacts:** `results/frozen_hard_tests/RARE_CONFLUENCE_QUIET_ENTRY_V2_20260811_17/`  
**Inventory:** `docs/research/ORDERBOOK_RESEARCH_SIGNAL_INVENTORY.md` §10
