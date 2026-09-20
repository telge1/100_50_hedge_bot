# History Extension Options — Phase 2B

**Generated (UTC):** 2026-09-05T13:59:26Z  
**Recommended primary option:** **Variante A**

## Variante A — Ältere Historie kausal erweiterbar (EMPFOHLEN)

### Exakter Zeitraum

| Segment | UTC | Role |
|---|---|---|
| Sofort rechenbar (research trades SoT) | **2026-07-19 → 2026-08-24T22:53Z** vor dem heutigen 7-Tage-Fenster | Backward extension of LLD_POOL episodes |
| Bereits materialisiert | 2026-08-25T00:05Z → 2026-08-31T23:01Z | Phase-1D frozen set (do not mutate) |
| Proven eligible edge before Phase-1D start | ab 2026-08-24T22:53Z | Coverage proof; Phase-1D started conservatively at 00:05Z |

Candles for LLD: `signal_generator.candles_1m` from **2025-12-11**.  
Binding price SoT for current builder: `btc_doge_research.research_public_trades` from **2026-07-19** through **2026-08-31**.

### Benötigte Quellen

- Candles 1m (LLD `export_snapshot`) — vorhanden  
- Research public trades (1s path) — vorhanden Jul19–Aug31  
- **Nicht** erforderlich für LLD_POOL V1 targets: OB200, Full OB, OI, Liq, Profile  

### Wiederverwendungslogik

- Same frozen episode contract `liquidity_destination_episode_contract_v1`  
- Same builder semantics (`available_at <= T0-60s`, nearest targets, 60m horizon, non-overlap)  
- Raise/relax **only** the operational `MAX_BOUNDED_EXPAND_HOURS=168` ceiling in a **future** implementation phase (not in this audit)  
- Multi-pass windows or higher max-hours; keep internal 2h trade chunks  
- Prefix-parity vs Phase-1D Aug25–31 required before merge  

### Erwartete Episodenzahl

Current rate ≈ 1719 / 7 ≈ **246 eligible/day** (both symbols).  
Jul19–Aug31 ≈ 44 days → rough order **~10k** eligible if rate holds (estimate only; not a guarantee).  
Jul19–Aug24 alone ≈ 36 days → roughly **~9k** additional before merging with 1719.

That would clear the **30-day / 5000-episode** model gate *if* quality gates and seam checks pass.

### Ressourcenrisiko

- Phase-1D 7 days ≈ 4–5 min BTC wall, ~100 MiB RSS, chunked  
- ~6× longer calendar ⇒ expect multi-hour single-process runs; keep `nice`, no parallel symbols initially  
- Candle lookback per T0 is the main CPU cost (export_snapshot)

### Notwendiger Paritäts-/Seam-Test

1. Prefix parity: extended run’s Aug25–31 semantic rows == Phase-1D fingerprint cores  
2. Idempotence on one multi-day chunk  
3. Confirm research trade `coverage_status` COMPLETE on extension window  
4. Document HTF tip residual unchanged  
5. **Do not** mix live-canonical trades into the same SoT without an explicit source contract  

### Kontrollierter nächster Prompt

```text
PHASE 2C – CAUSAL HISTORY EXTENSION (JUL19–AUG24)
- raise bounded expand hours only as needed
- rebuild episodes Jul19→Aug31 without mutating Phase-1D bytes
- prefix-parity gate vs fingerprint 9f7d2bd2…
- no features, no ML, no --now, no dashboard
```

---

## Variante B — Nur neue Historie (Sekundär / parallel)

### Ab wann vollständig?

Live collectors continue past Aug31 (OB200 raw, OI 5s, live trades, candles).  
**Research SoT for the episode builder still ends 2026-08-31** (`research_public_trades` Sep+=0).

### Collector-Status (read-only observation)

- OB200 raw shadow: Aug24 → Sep05 live  
- Live canonical trades: → Sep05 ~09:56Z (lag vs wall clock noted)  
- Live OI 5s: → Sep05 ~13:58Z  
- Research rematerialization for Sep+: **not done**

### Dauerhaft fehlend

- Continuous Full OB disk archive  
- OB1000 history  
- Pre-Aug24 full OB200 raw levels (seam)  

### Tägliches Anhängen

Requires either:

1. Research rematerialization of trades (+ optional OB/OI) — **DB writes / separate approval**, or  
2. Explicit dual-path source contract freezing live-canonical semantics  

Then append closed UTC days with horizon+1h lookback; reserve forward holdout untouched.

### Wann 30 Tage / 5000 Episoden?

If only forward from Sep01 at ~246/day: **~21 more days** to add ~5000, but need research SoT catch-up first. Combined with Variante A, model gate can be hit sooner **without** waiting for Sep rematerialization.

### Forward-Reservierung

After feature/model freeze: reserve **≥14 new closed UTC days** and **≥1000** eligible episodes never used in development.

---

## Variante C — Upstream-Lookahead gefunden

**Nicht gewählt.** Classic target lookahead (future lifetime/max/touch/end-state backprojection) was **not** found. Residual HTF tip invalidation is documented under PASS-with-caveat, not LOOKAHEAD_BLOCKED.

---

## Warum nur sieben Tage? (Kurzfassung)

1. Builder hard cap `MAX_BOUNDED_EXPAND_HOURS=168`  
2. Research trade SoT ends **2026-08-31**  
3. Conservative start **2026-08-25T00:05Z** (proven eligible from Aug24 22:53Z)  
4. Phase-0 common calendar also referenced OB200 start Aug24 — but OB200 is **not** required for LLD_POOL targets  

→ Seven days are an **operational window choice**, not “all causal history that exists.”
