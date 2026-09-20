# CURRENT_DIRECTION_FORECAST — Phase 0 Abschlussbericht

**Verdict:**

```text
CURRENT_DIRECTION_FORECAST_PHASE_0_CACHE_BRIDGE_REQUIRED
```

**Generated (UTC):** `2026-09-05T14:33:50Z`

Neue Forschungsfunktion, **getrennt** von `LIQUIDITY_DESTINATION_BIAS`. Keine Pool-Destination als Ziel. Phase 0 = Zugriff, Kausalität, Vertrag — keine Implementierung.

## 1. Git

| Repo | Branch | HEAD |
|------|--------|------|
| SR | `feature/btc-doge-research-db` | `9ab70ee955ff5a47e2c42ecaa6d727df581c2d34` |
| OA | `feature/strategy-lab-phase1` | `1019974694c27f01249718c99b192c8490038f49` |

Dirty vorbestehend (u.a. `dashboard/app.py`); unangetastet. Neu nur dieses Results-Verzeichnis. Keine `AGENTS.md` gefunden.

## 2. Full-OB Ringbuffer (Audit A)

- **Prozess:** Collector `bybit-full-ob-raw-archive-btc-doge.service`  
- **PID:** **1902763**  
- **Nicht** Dashboard; EdgeWatcher in-process  
- **Retention:** **~600 s (10 min)** — Code default + Runtime coverage ~600s  
- **Symbole:** BTCUSDT, DOGEUSDT  
- **Inhalt:** rohe Delta-Envelopes im Ring; rekonstruiertes Full Book separat im gleichen Prozess  
- **Zeiten:** Event (`ts`/`cts`) und Receive (`receive_time_ns`) getrennt  
- **Gaps:** Book `gap_count=0`, collector `sequence_gaps_total=0` zum Auditzeitpunkt; Overflow 0; je ~3001 Messages  
- **Eviction:** Zeitfenster dann max msgs/bytes  

## 3. CLI-Zugriff (Audit B) — Kernbefund

**Ein separater CLI-Prozess kann den bestehenden RAM-Ringbuffer heute nicht lesen.**

Unix-Socket liefert Live-`snapshot` (depth=0 Full Book) nach Lease — **ohne Pre-Roll**.  
FR-Pre-Roll-Flush nur bei Edge-Capture, nicht als generisches `--now`.

**Empfehlung:** Variante **B** (minimaler localhost RO-Bridge/Dump) + Socket für Live-Book. Variante D (eigene Full-OB-WS) vermeiden (`full_book_active_topics=2`).

## 4. T0-Inventar (Kurz)

- Full OB live + 10-min RAM pre-roll: ja im Collector; CLI: Book teilweise, Pre-Roll nein  
- Public trades canonical: vorhanden aber zum Audit **~4.5h stale** (max ~09:56Z vs ~14:31Z) → Pflicht-Freshness-Gate  
- OI 5s: frisch  
- Candles 1m: ebenfalls freshness gap (~09:55Z)  
- Research trades: nur bis 31.08.  
- Profile/POC full-session: Lookahead-Risiko → blockiert bis kausal  
- historical_full_ob_training_samples = **0**

## 5. Verträge

- Feature draft: `available_at <= T0`; Buckets `bucket_end < T0`; OI closed-only; kein Full-Session-POC  
- Outcome: Horizons 5/15/30/60; **primary 15**; Preis = **Last Trade**; Klasse UP/DOWN/NEUTRAL mit vorab eingefrorener Vol-Deadband-Variante empfohlen  
- Prediction immutable vor Observer; Outcomes getrennt  

## 6. Erster Output

**Variante 1 — Faktischer State Analyzer** zuerst (`BUY_PRESSURE`/`SELL_PRESSURE`/`MIXED`/`INSUFFICIENT_DATA`).  
Kein Richtungsanspruch, keine kalibrierte Confidence.

Variante 2 (unkalibrierter Heuristik-Lean) erst nach Bridge + Pilot und vorab eingefrorenen Gewichten.

## 7. Gates

- Technischer Pilot: ≥10 complete forecasts  
- Provisorische Heuristik: ≥200, multi-day  
- Final forward: ≥14 Tage, ≥1000 forecasts, frozen contracts  

Manueller CLI: Selection Bias → Shadow-Sampler für Bewertungsstichprobe.

## 8. Sicherheit

```text
CODE_CHANGED=false
TEST_CHANGED=false
DATABASE_WRITES=false
PROCESSES_CHANGED=false
DASHBOARD_CHANGED=false
COLLECTOR_CHANGED=false
COMMIT=false
PUSH=false
CLI_IMPLEMENTED=false
```

**STOP.** Keine CLI-Implementierung und keine Bridge-Implementierung in diesem Auftrag.
