# Next Phase Plan — after Phase 2B

**Verdict carried forward:** `LIQUIDITY_DESTINATION_HISTORY_LOOKAHEAD_V1_CAUSAL_EXTENSION_READY`  
**Chosen option:** Variante A  
**Live bias prognosis:** **NOT ALLOWED**

## Immediate STOP conditions (still active)

- no Phase-3 orderflow model
- no bias trading signal
- no ML
- no `--now`
- no dashboard / collector changes
- no backfill writes in this audit

## Phase 2C (next controlled prompt) — Causal History Extension

Goal: materialize additional LLD_POOL episodes **2026-07-19 → 2026-08-31** under the same causal contract, without mutating Phase-1D artifact bytes.

Steps:

1. Freeze extension contract addendum (window hours, SoT table unchanged).  
2. Implement operational raise of `MAX_BOUNDED_EXPAND_HOURS` only if required (code change phase — separate approval).  
3. Run BTC then DOGE sequentially, nice’d, chunked.  
4. Gates: exit 0, no overlapping eligibles, knowledge_cutoff=T0, available_at≤T0, NEITHER only with closed horizon.  
5. **Prefix parity:** Aug25–31 semantic core must match Phase-1D fingerprints.  
6. Publish new combined fingerprint; keep Phase-1D directory immutable.  
7. Re-evaluate history gates (expect model gate closer/clear if ≥30 days & ≥5000 episodes).  

Still forbidden in 2C: features beyond distances already frozen, ML, threshold search, live bias.

## Phase 2D — optional parallel Forward SoT decision

Only if Sep+ needed soon:

- Choose rematerialize research trades **or** freeze live-canonical dual-path contract  
- Seam/parity test mandatory  
- Separate approval for DB writes  

## Phase 3 (only after model history gate PASS)

- Causal feature contract using `feature_availability_audit.csv` SAFE rows  
- Always beat frozen `B1_NEAREST_TARGET` on non-consumed splits  
- Reserve untouched forward ≥14 days / ≥1000 episodes after full freeze  

## Explicit non-goals until gates pass

| Claim | Allowed now? |
|---|---|
| Technical feature plumbing on 7-day set | Yes (provisional) |
| Bias/model lift claim | No |
| Live bias prognosis | No |
| Treat 30/31 Aug as final holdout | No (consumed benchmark) |
