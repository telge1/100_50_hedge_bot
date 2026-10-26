# USER_CONFIRMED_RULES — Pool Landscape V1 (candidate freeze)

**Status:** `FREEZE_CANDIDATE` — not `FINAL_APPROVED`.

Authority: user decisions documented in `Signal_definierung.md` and echoed in this recovery task (2026-10-22). Implementation code is **not** authority.

## Data & landscape

| rule_id | Description | Source |
|---------|-------------|--------|
| DATA-01 | Symbol DOGEUSDT, timeframe 5m | Signal_definierung.md §Verbindliche Forschungsleitplanken |
| DATA-02 | PD1 for entry microstructure; PD10 noted for larger TP landscape (not merged) | Signal_definierung.md L7–8 |
| DATA-03 | Exact unique-origin density; min-depth **2** for this strategy line | Signal_definierung.md L8 |
| DATA-04 | Causal historical **ACTIVE** SSOT; ≥168h lookback; include older still-ACTIVE origins | Signal_definierung.md L5–6 |
| DATA-05 | Decisions use only pools/EMA known at decision time; entry after **closed** signal candle at **next 5m open** where stated | Signal_definierung.md L9 |

## SHORT

| rule_id | Description | Source |
|---------|-------------|--------|
| SHORT-01 | Entry at relevant **upper** touch pool | User task + Signal_definierung.md |
| SHORT-02 | Reaction on **completed** 5m candle; entry at next open when applicable | Signal_definierung.md L9, S3 |
| SHORT-03 | **Large upper pool gap** part of entry landscape (not yet fully algorithmic) | User task; S4/S5/N3 contrast |
| SHORT-04 | SL = **0.2%** above upper pool **upper** edge | Signal_definierung.md L7 |
| SHORT-05 | TP = **large/dense lower-density cluster** — **not** nearest lower pool or strongest single pool | Signal_definierung.md L7 |
| SHORT-06 | Min distance from **actual entry** to **TOP** of TP cluster ≥ **0.5%** | Signal_definierung.md L7 (revised from 0.8%) |
| SHORT-07 | Fees assumption round-trip **0.11%**; no slippage/funding in base doc | Signal_definierung.md L7 |

## LONG (mirror — partial)

| rule_id | Description | Source |
|---------|-------------|--------|
| LONG-01 | Relevant **lower** entry pool (mirror) | User task |
| LONG-02 | SL = **0.2%** below lower pool **lower** edge | User task |
| LONG-03 | TP = **large/dense upper-density cluster** (mirror) | User task |
| LONG-04 | Min distance entry to **BOTTOM** of TP cluster ≥ **0.5%** | User task |
| LONG-05 | **Long signal candle / trigger not fully specified** | OPEN — user task §3 |

## Market structure (manual sequence)

| rule_id | Description | Source |
|---------|-------------|--------|
| MS-01 | First relevant lower reached → large gap below → **WATCH_REVERSAL** (no long yet; block new shorts provisionally) | Signal_definierung.md Two-Lower-Pool §484–523 |
| MS-02 | Recovery → **new second lower** pool below → gap → **LONG_CANDIDATE**; check upper dense TP | Same |
| MS-03 | Lifting WATCH / resuming shorts **not sufficiently specified** | OPEN |
| MS-04 | Automatic 3B detector is **not** equated to validated user rule | Phase 3B verdict PARTIAL |

## Position & opportunity

| rule_id | Description | Source |
|---------|-------------|--------|
| POS-01 | **Max 1** open position; no new LONG/SHORT while open; raw signals still stored | User task; C011 §475–479 |
| POS-02 | New evaluation only after SL/TP exit | User task |
| POS-03 | Same upper opportunity may emit multiple raw signals; C010/C011/C013 = same opportunity (manual) | Signal_definierung.md §C |
| POS-04 | Re-entry after exit on same opportunity **not generally resolved**; S9/S10 precedent preserved | Signal_definierung.md L544 |

## Configuration values (parameters ≠ all hard gates)

| Parameter | Value | Hard gate? |
|-----------|-------|------------|
| min_pool_gap_pct | 0.5 | **OPEN** — user research; distinct from entry→TP 0.5% |
| min_pool_age_bars | 5 | **NOT confirmed** as hard exclusion (C003 counterexample label) |
| min_tp_distance_pct | 0.5 | **Confirmed** for TP distance |
| roundtrip_fees_pct | 0.11 | Confirmed assumption |
| max_open_positions | 1 | Confirmed |

## Pending limit execution (4E)

| rule_id | Description | Source |
|---------|-------------|--------|
| EXEC-USER-PENDING-LIMIT-V1 | SHORT sell-limit / LONG buy-limit at pool edges; pending until fill or pool depth-2 loss | Signal_definierung.md PENDING LIMIT V1 |
| EXEC-PENDING-TTL-01 | No fixed time-based expiry while zone valid | Same |
| EXEC-POOL-DEACTIVATED-01 | Cancel on confirmed depth-2 zone loss (3A SSOT lifecycle) | Same |

## Explicit non-goals (user)

- No profit optimization in discovery phase (Signal_definierung.md §388–392).
- No mixing with **Original LMV2** strategy.
- No new min-depth-1 entry types as production strategy (Signal_definierung.md §393).
