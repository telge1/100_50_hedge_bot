# Frozen Tier-A Filter Watchlist

**Status date (UTC):** 2026-08-20  
**Branch:** `research/confirmed-orderbook-entries`  
**Repo:** `orderbook_analyse`  
**Related inventory:** `docs/research/ORDERBOOK_RESEARCH_SIGNAL_INVENTORY.md` (§11)

## Purpose

Freeze **filters on existing Tier-A signals** as a research watchlist.

- **Not** a new entry strategy  
- **Not** live-confirm  
- **Not** live-alert  
- **No** silent retuning of thresholds  

| Candidate | Side | Status |
|---|---|---|
| `TIERA_LONG_ONLY_IN_HIGH_VOL_V1` | LONG filter; SHORT passthrough | **`PARTIAL_WATCHLIST`** |

## Source of Truth

- ClickHouse `signal_generator.signals` with `tier_a=1`
- `signal_outcomes`
- Versions:
  - `wave_fade_no_be50_v1`
  - `wave_fade_shadow_pipeline_v1`
- Outcome horizons:
  - `TRADE`
  - `TRADE_NO_BE50` (identical to `TRADE` in the primary window; no BE50 activations)

Feature / regime context (read-only inputs for filters):

- Prior audit parquet / candles / OB features as used in frozen filter tests
- **OI / liquidations: not used** in the primary window

## Data window (primary)

- **Evaluation:** `2026-08-11T00:00:00Z` … `2026-08-17T23:59:59Z`
- **Universe context:** 51 coins with clean `ob200_v3` coverage
- **Always report** OB-resolved subset separately (Tier-A includes symbols without full OB, e.g. ACE/XAU)
- **No data after Aug17** in this freeze
- **No OI/Liq**

## Candidate: `TIERA_LONG_ONLY_IN_HIGH_VOL_V1`

### Status

- Verdict artifact: `TIERA_LONG_ONLY_IN_HIGH_VOL_V1_PARTIAL`
- Watchlist label: **`PARTIAL_WATCHLIST`**
- **Kein Live-Confirm, kein Live-Alert**

### Rule (shared)

```text
SHORT  → always KEEP (passthrough)
LONG   → KEEP only if high_vol is active
         (no other conditions)
```

### Variant A — primary (regime-audit high_vol)

```text
LONG keep iff vol_ratio_15m >= 1.257911
```

- Exact same high_vol definition as regime audit: window tertile **p66** / `vol_regime == high_vol`
- Primary hypothesis test for this watchlist entry

### Variant B — practical a-priori comparison

```text
LONG keep iff vol_ratio_15m >= 1.0
```

- Not optimized against Variant A
- Simpler absolute threshold; more keep
- Closely related to prior `FROZEN_TIERA_LONG_FILTER_VOL_RATIO_15M_V1` (PARTIAL)

### Results — Variant A (TRADE; NOBE identical)

| Slice | Before → After | Notes |
|---|---|---|
| ALL WR | **64.2% → 73.5%** | +9.4 pp |
| LONG WR | **55.9% → 71.4%** | n_after resolved = **21** |
| LONG OB WR | **47.9% → 64.3%** | n_after resolved = **14** |
| SHORT WR | **74.5% → 74.5%** | unchanged; 0 removed |
| Removed all LONG | **18W / 20L** | losses > wins |
| Removed OB LONG | **14W / 20L** | losses > wins |
| LOO | positive LONG lifts | n still small |
| ACE/XAU | **not** sole driver | excl ACE/XAU same OB lift |

Kept/removed (A): **78 / 46** (removed reasons = LONG below p66 only).

### Results — Variant B (a-priori)

- ALL WR lift **≈ +10.5 pp**
- LONG OB lift **≈ +22.1 pp**
- More keep than A; simpler `>= 1.0` threshold

### Interpretation

- Tier-A **LONGs need high-vol / movement**; quiet / low vol LONGs underperform in this window
- Tier-A **SHORTs should stay unfiltered** here (universal vol filter previously hurt SHORT)
- Currently the **best practical Tier-A filter signal** found in this research line
- Still only **`PARTIAL_WATCHLIST`**: small post-filter n (especially LONG OB = 14) and only **7 OB days**

## Hard rules (retest / no silent retune)

1. Retest with a **new OB holdout after 2026-08-20** using **exact** Variant A threshold `1.257911`; report Variant B in parallel.
2. **Do not change thresholds** without a **new candidate name** (e.g. `TIERA_LONG_ONLY_IN_HIGH_VOL_V2`).
3. Adding OI/Liq = **new variant only**, e.g. `TIERA_LONG_ONLY_IN_HIGH_VOL_OI_V1` — never silently extend V1.
4. Watchlist ≠ live alert / live confirm.

## Related prior Tier-A work

| Artifact | Role |
|---|---|
| `results/frozen_signal_ob_feature_audit/20260811_17/` | OB feature audit on Tier-A |
| `results/frozen_signal_regime_audit/20260811_17/` | Regime buckets; defined high_vol p66 |
| `results/frozen_signal_filter_tests/FROZEN_TIERA_OB_FILTER_H1_VOL_RATIO_15M_20260811_17/` | Universal vol filter — **REJECTED** (hurts SHORT) |
| `results/frozen_signal_filter_tests/FROZEN_TIERA_LONG_FILTER_VOL_RATIO_15M_V1_20260811_17/` | LONG-only `vol>=1` — **PARTIAL** |

## Artifacts (this candidate)

| Path | Contents |
|---|---|
| `results/frozen_signal_filter_tests/TIERA_LONG_ONLY_IN_HIGH_VOL_V1_20260811_17/` | `summary.md`, `summary.json`, `kept_signals.csv`, `removed_signals.csv`, `metrics.csv`, LOO/day/coin, Variant B CSVs |

## Changelog

| Date (UTC) | Note |
|---|---|
| 2026-08-20 | Created; freeze `TIERA_LONG_ONLY_IN_HIGH_VOL_V1` as `PARTIAL_WATCHLIST` |
