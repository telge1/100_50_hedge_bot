# ABSCHLUSSBERICHT — BTC/DOGE Research Trade Final Audit v1

## Verdict

**BTC_DOGE_RESEARCH_TRADE_REMATERIALIZATION_READY**

## Branch / HEAD / Dirty

- Branch: `feature/btc-doge-research-db`
- HEAD: `2af0477dc4654d47603a2845a0ea1463b0bcfa12`
- Dirty files: 114 (kein Commit, kein Push)

## Importabschluss

- Build-ID: `8244abe0186ff712a7917eb66e397dd7c93cc28457aaf0d5a42652fe8c96e323`
- Completed: 1794 + Skipped: 318 = **2112** Segmente
- Rows written: **62,759,179**
- Failed: []
- Finished: 2026-09-03T10:56:51.165090Z

## Segmentstatus (ClickHouse Source of Truth)

- RUNNING: 0
- FAILED: 0
- MISSING_IMPORTABLE: 0
- CONFLICT: 0
- READY: 2100
- COMPLETE_EMPTY: 12
- Extra non-plan pilot segment: 1

## Physische vs. kanonische Rows

- Physical rows: 82031289
- Canonical rows (FINAL): 82031289
- Canonical duplicate keys: **0**
- Physical multi-version keys: 0
- Quarantined shifted rows: 85013

## UTC / −2h Audit

- Active SHIFT_MINUS_7200: **0**
- Active SHIFT_PLUS_7200: 0
- Active OTHER_TIMESTAMP_SHIFT: 0
- Active EXACT_0_SECONDS: 82031289
- Field mismatches on joined keys: 0

## Trade-Key- und Feldparität

- Joined keys (OA ↔ Research): 82031289
- BTC unique delta: 0
- DOGE unique delta: 0
- Price mismatches: 0
- Size mismatches: 0
- Side mismatches: 0
- Source gaps: 0
- Target gaps: 0

## Idempotenz und Recovery

- Idempotency: IDEMPOTENCY_PASS
- Terminal skip: IDEMPOTENT_SKIP
- Parallel runner block: ALREADY_RUNNING / LOCK_HELD

## Downstream / Source Purity

- Fight-CLI lädt Trades aus `btc_doge_research.research_public_trades FINAL`
- Companion standardmäßig deaktiviert; OA nur read-only für Paritätsaudit

## BTC Source-Pure Golden (2026-08-31T19:00Z)

- Exit: 0
- Validation pass: True
- Wall time: 39.7 s

## DOGE Source-Pure Golden (2026-08-31T13:00Z)

- Exit: 0
- Validation pass: True
- Wall time: 22.31 s

## Eligibility Regression

- 2026-08-27T06:42:23Z: expected `DATA_PARTIAL_FACTS_ONLY`, observed `DATA_PARTIAL_FACTS_ONLY`, exit 4 (PASS)
- 2026-06-01T12:00:00Z: expected `DATA_NOT_AVAILABLE`, observed `DATA_NOT_AVAILABLE`, exit 3 (PASS)
- 2026-08-31T19:00:00Z: expected `CONTEXT_PARTIAL`, observed `DATA_COMPLETE`, exit 0 (PASS) — OI day-tag PARTIAL visible; effective window density COMPLETE → DATA_COMPLETE acceptable

## Tests

```
.............................                                            [100%]
29 passed in 0.04s
```

## Collector / Live-Sicherheit

- ClickHouse ausschließlich read-only
- Kein Rematerialization-Runner aktiv
- Collector-PIDs unverändert dokumentiert in `preflight.json`

## Fight-CLI Performance (separater Track)

- BTC Golden Laufzeit: ~39.7 s (Ziel <10 s für Fight-CLI-READY, blockiert **nicht** Trade-Rematerialization-READY)
- DOGE Golden Laufzeit: ~22.31 s
- **Empfehlung:** Fight-CLI Performance-Optimierung (41 s → <10 s) darf als nächster Schritt starten, unabhängig vom Trade-Rematerialization-READY.

## Artefakte

Alle Outputs unter `results/btc_doge_research_trade_final_audit_v1/`.
