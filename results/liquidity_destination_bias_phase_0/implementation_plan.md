# LIQUIDITY_DESTINATION_BIAS — Implementation Plan (post Phase-0)

**Prerequisite:** explicit human Freigabe after Phase-0. No code in this document is authorization to implement.

---

## Storage recommendation (from Phase-0)

| Variant | Verdict for V1 |
|---------|----------------|
| A Continuous Full-OB raw | **Defer.** Scientifically strongest for resting far walls, but no measured continuous write cost yet; needs controlled 24h pilot. Selection-free, max disk/CPU risk on existing collector. |
| B RAM Full-OB + compact 1s features durable + raw Full-OB only for research episodes | **Preferred long-term ops shape** (mirrors FR today). Still requires feature schema design + 24h pilot for sizes. |
| C OB200/OB1000 (+ trades + LLD) | **Mandatory V1 historical common denominator.** OB1000 has **no history**. OB200 BTC span ~8 bps limits RESTING_WALL far targets → prefer LLD_POOL targets + OB200 features. |

**Default path:** implement Phases 1–4 on **Variant C** common denominator; use FR Full-OB only as optional enrichment / ablation; schedule separate Freigabe for Variant A/B 24h storage pilot before any collector change.

---

## CLI architecture (later)

### History builder
```bash
python scripts/build_liquidity_bias_history.py \
  --symbols BTCUSDT,DOGEUSDT \
  --horizon-minutes 60 \
  --block-class LLD_POOL \
  --require-complete
```
- Writes versioned episode parquet/jsonl under `results/liquidity_destination_bias_v1/...`
- Idempotent by `(symbol,T0,contract_version,feature_set_version)`
- Does **not** scan full raw forever on each `--now`

### Case runner
```bash
python scripts/run_liquidity_bias.py --symbol BTCUSDT --timestamp 2026-08-30T16:00:00Z --horizon-minutes 60 --require-complete
python scripts/run_liquidity_bias.py --symbol BTCUSDT --now --horizon-minutes 60 --require-complete
```
- Loads frozen contract + nearest historical cohort stats
- Emits bias + confidence + contradicting evidence + `RESEARCH_ONLY=true`
- Never places orders

Shared engine module (proposed): `research/liquidity_destination_bias/` in SR (consume OA pools/walls via imports, no strategy coupling).

---

## Baselines (required before any ML)

1. Always nearer block (bps)
2. Price momentum / prior return sign
3. Taker-delta sign at T0 window
4. Book imbalance sign
5. Class-frequency / random

Abort complex models if none beat nearer-block + delta after calibrated costs of complexity.

---

## Phases

### Phase 0 — Inventur und Contract (THIS)
- **Goal:** inventory, contract draft, plan
- **Files:** `results/liquidity_destination_bias_phase_0/*` only
- **Data:** read-only metadata / small SELECTs
- **Artifacts:** ABSCHLUSSBERICHT + CSVs + contract + plan
- **Tests:** n/a
- **Abort:** if no usable public trades OR no dual-target geometry source → BLOCKED (not the case)
- **Live risk:** none
- **Freigabe:** done when human accepts verdict

### Phase 1 — Dataset / Episode-Builder
- **Goal:** build frozen episodes with targets@T0 + first-touch labels for H∈{5,15,30,60}
- **Modules:** new `research/liquidity_destination_bias/{contract,eligibility,targets,path_label,builder}.py`; reuse LLD adapter, coverage_gate, trade buckets
- **Data access:** `btc_doge_research` trades/OB200 1s for **2026-08-24→08-31** overlap; optionally raw OB200 replay for book features; **no** Full-OB required
- **Artifacts:** episode store + coverage report + sample-size table
- **Tests:** no-leakage, frozen targets immutable, idempotent builder, touch label fixtures, ambiguity cases
- **Abort:** eligible N too small for any horizon after gates; or dual-path trade semantics unresolved for chosen window
- **Live risk:** CH read load — throttle; no writes outside research result dirs / optional research tables with Freigabe
- **Freigabe:** required before any CH DDL

### Phase 2 — Historischer CLI-Replay
- **Goal:** `run_liquidity_bias.py --timestamp` reproduces builder features/labels
- **Modules:** CLI + formatting (pattern from btc_ob_fight)
- **Artifacts:** case JSON/MD under results/
- **Tests:** prefix parity; timestamp reproducibility
- **Abort:** engine divergence builder vs CLI
- **Live risk:** none
- **Freigabe:** soft

### Phase 3 — Regelbasierte Bias-Baseline
- **Goal:** transparent rules vs §baselines; report accuracy / ECE-ish calibration bins
- **Modules:** `rules_v1.py`, evaluation harness
- **Artifacts:** metrics tables; **no** threshold fishing on full sample — freeze rules on train window first
- **Tests:** time-split train/val/forward; outcome not used to retune on forward
- **Abort:** no lift vs nearer-block after freeze
- **Live risk:** none
- **Freigabe:** required to declare “baseline interesting”

### Phase 4 — Statistischer Vergleich / kalibrierte Wahrscheinlichkeit
- **Goal:** simple probabilistic model (logistic/GBM) on **common-denominator features only**
- **Modules:** train script + calibration
- **Artifacts:** model card; feature list hash; forward metrics
- **Tests:** leakage; train/serve parity; sample-size gate
- **Abort:** no calibrated lift; Full-OB feature contamination detected
- **Live risk:** none
- **Freigabe:** required

### Phase 5 — `--now` Shadow-Bewertung
- **Goal:** live shadow classification using same engine; freshness SLOs
- **Data:** live trades canonical + OB200/OB1000 on-demand **only if feature set allows**; never silently switch to Full-OB-only features
- **Tests:** freshness; INSUFFICIENT_DATA when lag high
- **Abort:** source contract for live trades not frozen
- **Live risk:** read-only APIs; **no orders**; avoid hammering collectors
- **Freigabe:** required

### Phase 6 — Prospektiver Frozen Forward Test
- **Goal:** pre-registered rules/model on future window
- **Abort:** any post-hoc target move or threshold edit
- **Live risk:** none beyond shadow
- **Freigabe:** required; publish protocol first

### Phase 7 — Optional Dashboard Button
- **Goal:** research UI only, RESEARCH_ONLY banner, no trading hooks
- **Abort:** if users confuse with entry signal
- **Live risk:** UI misuse
- **Freigabe:** separate product Freigabe

---

## Validation matrix (must-have tests)

- No future leakage / frozen targets
- First-touch + simultaneous labels
- Prefix parity builder↔CLI↔future dashboard
- Snapshot/delta replay + sequence gaps
- OB coverage bounds (`edge_book_coverage`)
- Idempotent history builder
- `--timestamp` reproducibility; `--now` freshness
- Sample-size gate; calibration sanity
- Fees not mixed into first-touch metric
- Time-separated train/val/forward
- Explicit distinction: prediction skill vs trading PnL

---

## Open decisions (recommended defaults)

1. Block class V1: **LLD_POOL** (default)
2. Primary horizon: **60m** (+ sensitivity columns)
3. Price path: **trade 1s buckets**
4. Touch: **near-edge pure touch** (acceptance secondary)
5. Storage: **Variant C for history**; A/B only after 24h pilot Freigabe
6. Live trades source: freeze **one** path (`research_*` after catch-up **or** `public_trades_canonical`) before Phase 5
