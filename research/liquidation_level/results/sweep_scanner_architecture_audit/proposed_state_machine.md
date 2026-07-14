# Proposed Research State Machine (Design Only)

## Purpose

After a validated **upper 50x immediate reclaim** sweep, activate an **analysis window**.
The sweep is **not** an entry. Entry only after separate 5m structure / price-action / momentum confirmation, with HTF context as gate — thresholds deferred.

## States

```
IDLE
 → SWEEP_DETECTED
 → ANALYSIS_ACTIVE
 → SHORT_CONFIRMED | LONG_CONTINUATION_CONFIRMED | INVALIDATED | EXPIRED
```

Terminal states do not auto-reenter without a new sweep trigger (research default).

---

### IDLE

| Field | Spec |
|-------|------|
| Entry | No active analysis; waiting for winner-class sweep |
| Duration | Unbounded |
| Data | Winner event stream + 5m clock |
| Transitions | → `SWEEP_DETECTED` on SweepTriggerEvent |
| Causal time | Continuous between closed 5m bars |

### SWEEP_DETECTED

| Field | Spec |
|-------|------|
| Entry | `SweepTriggerEvent` accepted (upper, primary 50x, immediate reclaim, frozen config_id) |
| Duration | Instantaneous bookkeeping state (zero or one closed bar) |
| Data | Event fields + **freeze** last-closed 30m/15m/5m context as-of sweep decision_time |
| Transitions | → `ANALYSIS_ACTIVE` after freeze committed |
| Causal time | At/after sweep bar close; HTF = last **closed** buckets only |

### ANALYSIS_ACTIVE

| Field | Spec |
|-------|------|
| Entry | From `SWEEP_DETECTED` |
| Duration | Variant windows of **next closed 5m candles after sweep**: **3 / 6 / 12** (all retained as research variants; **no selection yet**) |
| Data | Frozen HTF snapshot + rolling post-sweep 5m closes/structure/PA/momentum features |
| Transitions | → `SHORT_CONFIRMED` / `LONG_CONTINUATION_CONFIRMED` / `INVALIDATED` / `EXPIRED` |
| Causal time | Evaluated only on newly **closed** 5m bars (ages 1..N relative to sweep) |

Window variants (document only):

| Variant | Observed closed 5m after sweep | Max wall time |
|---------|--------------------------------|---------------|
| W3 | 3 | 15 minutes |
| W6 | 6 | 30 minutes |
| W12 | 12 | 60 minutes |

### SHORT_CONFIRMED

| Field | Spec |
|-------|------|
| Entry | Analysis path classifies **short reversal** with required 5m confirmation stack (rules TBD) |
| Duration | Terminal for this event (or holds until explicit entry audit consumes it in later phase) |
| Data | Confirmation evidence pack (5m mandatory; 15m/30m non-blocking or soft) |
| Transitions | Terminal → later Phase F entry audit (next-open) — outside this SM |
| Causal time | Confirmation candle close; entry not before next open |

### LONG_CONTINUATION_CONFIRMED

| Field | Spec |
|-------|------|
| Entry | Analysis classifies **bullish acceptance / continuation** after upper sweep (rules TBD) |
| Duration | Terminal for this event |
| Data | Acceptance/retest/HH-HL/momentum evidence + HTF upside context |
| Transitions | Terminal → later long entry audit if ever studied |
| Causal time | Confirmation candle close |

### INVALIDATED

| Field | Spec |
|-------|------|
| Entry | Hard conflict or structural invalidation during window (e.g. strong opposing HTF block + broken local thesis — thresholds TBD) |
| Duration | Terminal |
| Data | Invalidation reason codes |
| Transitions | None |
| Causal time | On invalidating closed bar |

### EXPIRED

| Field | Spec |
|-------|------|
| Entry | Window length consumed without confirm or invalidate |
| Duration | Terminal |
| Data | Partial evidence dump for unclear class |
| Transitions | None |
| Causal time | After last allowed closed candle evaluated |

---

## Reverse vs Breakout vs Unclear (classification intent)

No thresholds. Evidence classes only.

### A. SHORT-REVERSAL path (examples of feature roles)

- Close / acceptance back under sweep/cluster level
- Failed breakout (structure or PA)
- LH formation; bearish CHoCH/BOS on 5m
- DI− / ADX context not strongly bullish
- Bearish momentum confirmation after PA
- 30m **not** strong bullish expansion blocking shorts
- 15m not accelerating counter to short thesis

### B. BULLISH-BREAKOUT / CONTINUATION path

- Acceptance above sweep/cluster level
- Retest holds as support
- HH/HL; bullish BOS/CHoCH
- Bullish momentum
- 15m/30m upward regime supportive

### C. UNCLEAR

- Conflicting HTF vs LTF
- No structure break within window
- Momentum ambiguous / no PA arm
- Window expires → often maps to `EXPIRED` with label `unclear`

---

## Timeframe roles (no thresholds)

### 30m — regime / blocker

- Overarching regime, main trend, strong HTF structure
- **May block** a short-reversal confirmation when HTF regime/structure is strongly bullish / expanding up (exact labels later)
- **May block** long-continuation when strongly bearish (if studied)
- Never alone triggers entry

### 15m — mid structure / quality

- Pullback vs continuation quality; trend quality; counter-trend activity
- **Confirms or neutralizes** a 5m thesis; soft veto / confidence modifier
- Never alone triggers entry

### 5m — reaction / timing (mandatory for entry)

- Sweep reaction, reclaim/acceptance, local BOS/CHoCH, PA, momentum, entry timing
- **Required** for `SHORT_CONFIRMED` / `LONG_CONTINUATION_CONFIRMED`
- Context-only features that **must never alone** trigger entry: raw LuxAlgo sweep, cluster strength, volume spike, HTF regime label, ADX alone, liquidation `liquidity_sweep_*` name collision events

### Context vs confirmation vs invalidation vs entry

| Role | Examples |
|------|----------|
| Context | Frozen 30m/15m regime, EMA stack, ATR%, level strength, cluster center |
| Confirmation | 5m PA confirm + momentum confirm + local structure shift aligned with path |
| Invalidation | Protective/structure breaks against thesis; hard HTF block; PA/momentum invalidate |
| Entry | Only after confirmed path; timing = **next 5m open after confirmation close** (Phase F) |

## Explicit non-goals now

- No concrete numeric thresholds
- No selection among W3/W6/W12
- No wiring into live `pipeline_audit` entry path
- Do not reuse existing `momentum` window as the sweep analysis window without redesign (momentum ages 0..3 **after PA**, not after sweep)
