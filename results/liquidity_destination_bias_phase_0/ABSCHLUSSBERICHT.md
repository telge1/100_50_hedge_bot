# LIQUIDITY_DESTINATION_BIAS — Phase 0 Abschlussbericht

**Generated (UTC):** `2026-09-05T10:51:48Z`  
**Verdict:** `LIQUIDITY_DESTINATION_BIAS_PHASE_0_READY_WITH_EXPLICIT_GAPS`

Phase 0 klaert Inventur, Machbarkeit und Contract. Es ist **kein** Nachweis eines profitablen Bias und **keine** Implementierungsfreigabe.

---

## 1. Verdict

`LIQUIDITY_DESTINATION_BIAS_PHASE_0_READY_WITH_EXPLICIT_GAPS`

**Warum nicht BLOCKED:** Es existieren (a) kausale Pool-Geometrie (LLD), (b) Public Trades fuer First-Touch-Pfade, (c) OB200-Roharchiv + Research-Snapshots fuer Book-Features innerhalb der Book-Span, (d) wiederverwendbare Coverage-/Eligibility-/Wall-/AEF-Bausteine, (e) ein klarer CLI-/Phasenplan.

**Warum WITH_EXPLICIT_GAPS:** Continuous Full-OB fehlt historisch; Flight Recorder ist edge-selektiv; BTC OB200 span ~8 bps limitiert RESTING_WALL-Fernziele; Research-DB Trails enden 2026-08-31 waehrend Raw/Live weiterlaufen; `research_market_1m/1s` nur Pilotfenster; Public-Trades Dual-Path; kein gemessener 24h-Speicherpilot fuer Variant A/B.

---

## 2. Git-Status beider Repositories

```
SR_BRANCH=feature/btc-doge-research-db
SR_HEAD=f7125a7ec9e3e455390234fb496ee40db3be15cd
SR_DIRTY=170
OA_BRANCH=feature/strategy-lab-phase1
OA_HEAD=bcf13edb8613570ed3c5addab6af08f93d99f45e
OA_DIRTY=273
```

| Repo | Branch | HEAD | Dirty |
|------|--------|------|-------|
| SR `spread_recovery_hedge_short_dev` | `feature/btc-doge-research-db` | `f7125a7ec9e3e455390234fb496ee40db3be15cd` | ja (viele bestehende User-Aenderungen; **nicht** angefasst) |
| OA `orderbook_analyse` | `feature/strategy-lab-phase1` | `bcf13edb8613570ed3c5addab6af08f93d99f45e` | ja (bestehend; **nicht** angefasst) |

Keine `AGENTS.md` in beiden Roots gefunden. Bestehende Dirty-Worktrees wurden weder bereinigt noch ueberschrieben.

---

## 3. Bestaetigung nicht ausgefuehrter Live-/Destructive-Aktionen

| Check | Status |
|-------|--------|
| Produktionscode geaendert | **nein** (nur `results/liquidity_destination_bias_phase_0/`) |
| DB Writes / DDL | **nein** (nur SELECT/DESCRIBE) |
| Collector start/stop/restart | **nein** (nur `systemctl --user is-active` read-only) |
| Dashboard Aktionen | **nein** |
| Trading / Orders / Signale | **nein** |
| Paketinstallationen | **nein** |
| Commit / Push | **nein** |
| Grosse Full-History Scans | **nein** (min/max/count + begrenzte 1h-Samples) |
| `system.tables` auf kaputtes `orderbook_analysis.orderbook_deltas` | bewusst vermieden / Fehler dokumentiert |

---

## 4. Belegte Dateninventur

Siehe `data_inventory.csv` (vollstaendige Tabelle). Kurzfassung:

| Quelle | BTC | DOGE | First-Touch? |
|--------|-----|------|--------------|
| OB200 raw shadow | 2026-08-24 → 2026-09-05 live (~2.7GiB, 561 files) | parallel (~1.0GiB, 561) | ja, eingeschraenkt (Depth 200) |
| OB200 CH 1s `research_ob200_snapshots_1s` | 2026-08-24 → **2026-08-31** (608951) | gleiche Spanne (608944) | ja, bis Aug 31 |
| OB1000 | live keeper/socket only | same | **nein historisch** |
| Full OB continuous disk | **existiert nicht** | — | — |
| Full OB FR episodes | 2026-09-03…05 episodisch | same (15 roots total) | ja, stark selection-biased |
| Trades research DB | 2026-07-19 → **2026-08-31** (68.97M) | 13.06M | **ja (primaer historisch)** |
| Trades live canonical | → 2026-09-05 ~09:56Z (~80.9M) | ~14.8M | ja mit Source-Contract |
| OI 5s live | → 2026-09-05 | → 2026-09-05 | Kontext |
| Liq live | → 2026-09-05 | sparse | Kontext |
| Features 1s v2 | → **2026-08-28** | same | ja bis Aug 28 |
| Market 1m/1s research | nur Pilotstunden | Pilot | **nein** als alleinige History |

---

## 5. Status OB Full, Ringbuffer, Flight Recorder

**Continuous Full-OB Storage:** nein. `FullBookOnDemandManager` ist explizit **RAM only**. Dauerhaftes Roharchiv ist **OB200** (`ob200_v3_live_archive`).

**Ringbuffer:** `BoundedRawRingBuffer` Defaults **10 Minuten**, max **50 000** Messages, **256 MiB** (`FlightRecorderSettings`).

**Flight Recorder:** speichert nur bei Profile-Edge **CROSS_IN** Event-Pakete unter  
`data/orderbook_raw_shadow/full_ob_edge_flight_recorder/`  
(Inhalt u.a. `full_ob_raw_deltas.jsonl.zst`, REST snapshot, manifests, `sequence_integrity.json`; Public-Trades-Datei in V1 Platzhalter/leer). Nested Profile Signals koennen **innerhalb** offener Captures entstehen — das ist **kein** Continuous-Archive und **kein** generischer Research-Trigger fuer beliebige LDB-T0s.

**Historische Full-OB Rohdaten:** belegt nur FR-Episoden **2026-09-03 bis 2026-09-05**, ~310 MiB Tree, 15 Fight-Roots (+ cont-Segmente).

---

## 6. Historisch eligible Zeiträume (kausaler First-Touch)

**Praktisches Overlap-Fenster fuer Variant-C History V1:**

```text
2026-08-24T22:47Z  →  2026-08-28T16:26Z   (OB200 raw/CH + features_1s + research trades)
2026-08-28T16:26Z  →  2026-08-31T23:59Z   (OB200 + trades; features_1s lueckenhaft/ende)
```

Danach bis Sep 5: Raw OB200 + live OI/Liq/Trades-canonical vorhanden, aber **Research-DB Materialisierung** und Features nicht catch-up — fuer `--require-complete` Research-Path erst nach explizitem Rematerialisierungs-/Source-Contract.

**FR Full-OB Fenster** nur als optionale Enrichment-Kohorte (Selection Bias).

**RESTING_WALL Fernziele:** BTC Median Book-Span im Pilot **~8.08 bps** (CH `research_orderbook_ob200_snapshots`, 2026-08-31 18:30–19:30, genuine 200×200). Beispielziele ~30+ bps vom Mid liegen **ausserhalb** OB200 → hard ineligible fuer RESTING_WALL-Klasse ohne Full-OB.

Metadaten zur Book-Abdeckung: tiefste Bid-/Ask-Preise aus Level-Arrays ableitbar; dedizierte persistente `coverage_bps` Spalte nicht noetig — Gate via `edge_book_coverage.py` Logik.

---

## 7. Wiederverwendbare Komponenten

Siehe `reusable_components.csv`. Kernstack:

1. **Targets:** OA `liquidity_pool_signal` + lifecycle `_first_destination` (adapt)
2. **Book gates:** SR `edge_book_coverage` + `coverage_gate` / `eligibility_contract`
3. **Walls/refill/consume:** SR `wall_events`, `edge_region_consumption`; OA arrival wall monitor / Stage A–B
4. **Aggressor:** OA AEF + SR `aggression_facts`
5. **Features:** OA `orderbook_v2.features` microprice/imbalance
6. **CLI pattern:** `btc_ob_fight` results contract

**Ungeeignet als Kernkopplung:** A+ Scanner `TF_LIQUIDITY=30m` / setups; FR Edge als einzige Sampling-Quelle.

---

## 8. Harte Datenluecken

1. Kein continuous Full-OB Disk Archive  
2. OB1000 ohne Historie  
3. Research trades/OB200 CH enden 2026-08-31; Features 2026-08-28  
4. Dual Public-Trades Pfade; Backfill BLOCKED (`PUBLIC_TRADES_BACKFILL_PIPELINE_PARTIALLY_READY`)  
5. `research_market_1m/1s` nur Pilot  
6. FR Selection Bias + leere `public_trades_raw` in FR V1  
7. BTC OB200 Span zu eng fuer viele RESTING Fernbloecke  
8. Kaputte CH-Tabelle `orderbook_analysis.orderbook_deltas` stoert manche Meta-Queries  

---

## 9. Vorgeschlagener kausaler Research-Vertrag

Siehe `research_contract_v1_draft.md`.

Defaults: **LLD_POOL** Targets; Trade-1s Path; Near-Edge Touch; Horizont 60m (+5/15/30); striktes Freeze@T0; keine A+-Kopplung; Common-Denominator gegen Full-OB Train/Serve Skew.

---

## 10. Empfohlene Speicherstrategie

- **V1 History:** Variant **C** (OB200 + Trades + LLD)  
- **Ops-Zielbild spaeter:** Variant **B** (RAM Full + kompakte 1s Features + episodische Raw-Captures)  
- **Variant A** nur nach **separatem 24h-Piloten** mit gemessenen GB/Tag, CPU, Queue-Overflow — **keine erfundenen Speicherzahlen**. Disk frei derzeit ~345 GiB auf `/` (df); OB200 shadow ~3.7 GiB; FR ~310 MiB — das ersetzt keinen Full-OB-Schreibpilot.

---

## 11. CLI-Architektur

Siehe `implementation_plan.md`: getrennte `build_liquidity_bias_history.py` und `run_liquidity_bias.py` (`--timestamp` / `--now`), versionierter Episode-Store, RESEARCH_ONLY, keine Orders.

---

## 12. Implementierungsphasen

Phase 0 (hier) → 1 Episode-Builder → 2 CLI Replay → 3 Regel-Baseline → 4 kalibrierte Prob. → 5 `--now` Shadow → 6 Frozen Forward → 7 optional Dashboard.  
Jede Phase mit Abort-Kriterien und Freigabe — Details in `implementation_plan.md`.

---

## 13. Test- und Leakage-Schutz

Pflicht: No-Future-Leakage, Frozen Targets, Touch/Ambiguity Labels, Prefix-Paritaet, Replay/Gaps, Coverage Bounds, Builder-Idempotenz, Timestamp-Repro, Now-Freshness, Sample-Size Gate, Kalibrierung, Fees≠Outcome, zeitliche Splits, **Prediction ≠ PnL**.

---

## 14. Ressourcen- und Live-Risiken

- History Builder kann bei naivem Full-Raw-Scan CH/CPU belasten → segmentiert, throttled, Prefer research tables  
- Collector-Aenderungen (A/B Storage) brauchen eigene Freigabe; Crash-Risiko am Live-FR/OB200 Pfad  
- `--now` darf Collector nicht mit Last killen (Socket Snapshots ok; kein zweites Full subscribe ohne Kapazitaetscheck)  
- Dashboard-Button erst Phase 7 — Verwechslungsrisiko mit Entry-Signal  

**Offener Last-Test (nicht ausgefuehrt):** 24h Full-OB continuous write Pilot; voller Gap-Audit Sep 1–5 Research rematerialization.

---

## 15. Offene Entscheidungen (empfohlene Defaults)

| Entscheidung | Default |
|--------------|---------|
| Block-Klasse V1 | `LLD_POOL` |
| Primary Horizon | 60m |
| Price path | public-trade 1s buckets |
| Touch | near-edge pure touch |
| Storage V1 | Variant C |
| Live trades source | vor Phase 5 **einen** Pfad freezen |
| Profil/VAH/VAL | nur optionales Kontext-Feature |
| ML | erst nach Baseline-Lift |

---

## Artefakte in diesem Ordner

- `ABSCHLUSSBERICHT.md` (diese Datei)
- `data_inventory.csv`
- `reusable_components.csv`
- `feature_inventory.csv`
- `research_contract_v1_draft.md`
- `implementation_plan.md`
- `_git_facts.txt`, `_generated_at_utc.txt`

---

## Compliance Footer

```text
PRODUCTION_CODE_CHANGED=false
DATABASE_WRITES_EXECUTED=false
COLLECTOR_ACTIONS_EXECUTED=false
DASHBOARD_ACTIONS_EXECUTED=false
TRADING_ACTIONS_EXECUTED=false
COMMIT_CREATED=false
PUSH_EXECUTED=false
DESTRUCTIVE_ACTIONS_EXECUTED=false
```

**STOP.** Warte auf ausdrueckliche Freigabe bevor Implementierung (Phase 1+) beginnt.
