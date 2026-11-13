# DOGE-LIVE-P10 — 5m-PD1-Publisher-Backlog: Ursache, Stabilisierung, Monitoring

**Datum:** 2026-11-12  
**Scope:** `doge-pd1-5m-live-publisher.service`, kanonische Kette unverändert (PD1/SSOT/D2)  
**Verdikt:** **PARTIAL** — Ursache belegt, minimaler Fix deployed & am Tail gefangen; **Backlog-Batch-Pfad in Live** noch nicht in Prod-Logs beobachtet (pending=0); Browser-Canvas nicht authentifiziert.

---

## 1. Tatsächliche Ursache (Phase A)

### 1.1 systemd / Prozess

| Signal | Wert |
|--------|------|
| Unit | `doge-pd1-5m-live-publisher.service` (user) |
| Exec | `--catch-up --live --batch-size 50 --poll-sec 20` |
| **NRestarts** (P10) | **0** (erster Start ~12:52 CET, ein kontrollierter Restart P10 ~18:00 CET) |
| Result | success |
| RAM | ~171–312 MiB peak; Swap-Touch ~14 MiB |

Keine Fehler-/WARNING-Spur im Journal 16:00–16:45 UTC; ausschließlich `live publish` + gelegentlich Collector-HTTP.

### 1.2 Messbeleg: Live-Modus ≈ 1 Close pro 5 min Wandzeit

Aus `journalctl` (Server-Log **CET**, `decision_close` **UTC**):

| Wand (CET) | `live publish` (UTC) | Aktivität im Journal |
|------------|----------------------|----------------------|
| 16:30:10 | 15:25:00 | — |
| 16:35:14–16 | 15:30:00 | Collector-HTTP **2 s** vor Publish |
| *Zwischen 16:30:10 und 16:35:14* | — | **~5 min Stille**, dann ~2 s bis Publish |

Interpretation:

1. **`process_rolling_close`** (PD1-Engine + CH-Publish + Catalog) benötigte im Rückstandsfall **~4,5–5 min Wall-Time** pro Close unter Last — nicht Quellen-Latenz (1m/5m voraus).
2. Der **Live-Loop** verarbeitete historisch **genau einen** pending Close pro Zyklus und schlief danach (`poll_sec=20`).
3. **Ankunftsrate** neuer 5m-Closes: **1 / 5 min** → **Durchsatz 1 / 5 min** ⇒ **kein Netto-Catch-up** ⇒ stabiler Rückstand **~65 min (~13 Kerzen)** (P9), bis parallel die Kante „von selbst“ nachrückte.

**Ausgeschlossen als Primärursache (gemessen):**

- Vollscan `list_published_closes` (~56k Zeilen): **~43 ms** (nicht Minuten).
- Publisher-Locks/Restarts: keine.
- Fehler-Retries im Journal: keine sichtbar.

### 1.3 Catch-up vs. Live (gleiche kanonische Engine)

Aus `results/doge_live_p4_live_publisher_v1/publisher.log` (Startup 2026-11-12):

| Modus | Beobachtung |
|-------|-------------|
| **Catch-up `run_batch(50)`** | ~**2,0–2,5 min** für 50 Closes ⇒ **~2,4–3,0 s/Close** |
| **Live Einzel-Close** (65 Intervalle) | Median **307 s**, P95 **309 s**, Max **2400 s** zwischen `live publish`-Zeilen |

**Schluss:** Nicht unterschiedliche PD1-Logik, sondern **Live-Orchestrierung (1× `process_rolling_close` + Warten)** gegenüber **Batch-Pipeline `run_rolling_controlled_pipeline`**.

### 1.4 Ressourcen / MC3-Einfluss (Korrelation, nicht isoliert kausal gemessen)

| Ressource (P10 Snapshot) | Wert |
|--------------------------|------|
| Load avg | ~3,1–4,2 |
| RAM | 5,4 GiB used / 17 GiB |
| **Swap** | **2,0 GiB voll** |
| MC3E BNB Worker | PID 1629440, **~87 % CPU**, ~960 MiB RSS, seit Nov-10 |
| MC3D SOL Monitor | bash läuft; **SOL Full-Worker nicht in ps** (nur Monitor) |

Unter dieser Last korreliert **~5 min/Close** im Live-Pfad; Batch-Catch-up am Morgen war deutlich schneller (geringere oder amortisierte Last). **Kein STOP der BNB/SOL-Worker** (Auftrag).

---

## 2. Throughput (Phase B)

| Metrik | Live (Rückstand) | Catch-up Batch |
|--------|------------------|----------------|
| Mittlere Wall-Zeit/Close | **~332 s** (Log-Intervalle) | **~2,7 s** (50er-Batches) |
| Median | **307 s** | ~2,5 s |
| P95 | **309 s** | — |
| Max | **2400 s** (ein Ausreißer) | — |
| Closes / min (effektiv) | **~0,18** | **~18–25** (ex post Batch) |
| Max. beobachteter Backlog | **~65 min** (P9 @ 16:35 UTC) | — |
| Catch-up-Geschwindigkeit | **≈ 0** (netto) im Live-Einzelmodus | P9 später bis **16:35** SSOT |

**Fazit:** Live-Einzelmodus **kann den Eingang nicht dauerhaft schneller abarbeiten** als 1 Close / 5 min Wall-Zeit unter der beobachteten Last → Engpass: **Live-Tick-Serialisierung + lange Close-Laufzeit**, nicht fehlende 5m-Quelldaten.

---

## 3. Minimaler Korrektur (Phase C)

**Voraussetzung:** Ursache 2+3 (Live 1×/Zyklus vs. 5 min/Close-Verarbeitung) durch Journal + Log-Intervalle belegt.

**Änderungen (keine neue Engine, keine SSOT-Änderung):**

| Datei | Inhalt |
|-------|--------|
| `published_reader.py` | `max_published_decision_close()` — O(1) High-Water |
| `common.py` | `pending_closes_after(pub_max, …)` — Pending ohne 56k-Liste |
| `run_doge_p4_live.py` | Wenn **`len(todo) > 1`**: kanonisches **`run_batch(chunk)`** (wie Catch-up); sonst unverändert `process_rolling_close`; **Laufzeit-Metriken** in State/Log |

**Regeln eingehalten:** gleiche `run_batch` / `process_rolling_close`, keine parallelen Doppel-Publishes pro Symbol/Zeitpunkt, keine Close-Lücken, keine Trading-Änderungen.

**Deployment:** `systemctl --user restart doge-pd1-5m-live-publisher.service` (P10); Dienst **active**, Tail **16:55 UTC**, `pending_closes=0`.

**Hinweis:** Der neue Backlog-Batch-Zweig braucht **`pending > 1`** — nach Restart sofort nicht getriggert (nur Catch-up `batch 0 processed=1`). **Verifikation des Batch-Zweigs ausstehend** beim nächsten echten Multi-Pending-Ereignis.

---

## 4. Live-Lag-Monitoring (Phase D)

Bereits vorhanden via `--status` / `publisher_state.json`:

| Metrik | Vor P10 | Nach P10 |
|--------|---------|----------|
| `last_complete_5m_close` | ✓ | ✓ |
| `inventory.max_decision_close` | ✓ | ✓ |
| `pending_closes` | ✓ | ✓ |
| `publish_lag_sec` | ✓ | ✓ |
| Collector-Health | ✓ | ✓ |
| **Neu:** `last_close_elapsed_sec` / `last_close_timing` | — | ✓ (Single-Close) |
| **Neu:** `last_batch_elapsed_sec`, `last_batch_per_close_sec` | — | ✓ (wenn Batch) |
| **Neu:** Log `elapsed_sec=` bei `live publish` | — | ✓ |

**Befehl:**  
`python3 tools/doge_live_p4_live_publisher_v1/run_doge_p4_live.py --status`

Keine zusätzliche Monitoring-Infrastruktur.

---

## 5. Frontend (Phase E)

| Prüfung | Ergebnis |
|---------|----------|
| `doge_5m_pd1_viewer_bounds()` `data_available_to` | **2026-11-12T16:55:00Z** |
| Publisher `max_decision_close` | **2026-11-12 16:55:00** |
| Parität Bounds ↔ Publish-Tail | **JA** |
| Browser Canvas / Tile-Cache | **nicht gemessen** (P9-B-Risiko bleibt) |

Bei aktuellem Publisher: **kein Cache-Fix** ohne nachgewiesenen Client-Fehler.

---

## 6. Regression & Last (Phase F)

| Test | Ergebnis |
|------|----------|
| Doppelte Publishes (CH, 2 h um Tail) | **0** Duplikate |
| Publisher nach Restart | Catch-up **1** Close → **WAITING**, lag **0** |
| MC3E BNB Worker | unverändert laufend |
| Collector | LIVE, WS true |
| Manuelle Trading-Boxen | nicht angerührt |
| Isolierter künstlicher Backlog-Replay | **nicht durchgeführt** (kein produktives Zurückhalten ohne Freigabe) |
| 30m/1h/4h On-Demand | nicht in dieser Session smoke-getestet |

---

## 7. Vorher / Nachher

| Zeitpunkt | Publish-Tail | Pending | Publish-Lag | Nutzer-sichtbarer Pool-Rückstand (P9) |
|-----------|--------------|---------|-------------|--------------------------------------|
| ~16:35 UTC (P9) | ~15:30 | >0 | ~65 min | ~5–25 min (Chart vs. Pools) |
| ~16:42 UTC (P9 nach Catch-up) | 16:35 | 0 | ~1 Kerze | reduziert |
| ~17:01 UTC (P10) | **16:55** | **0** | **0** | Bounds = Tail |

---

## 8. Offene Risiken

1. **Backlog-Batch-Pfad** noch ohne Prod-Log-Zeile `live backlog batch` — Verhalten bei `pending>1` beobachten.
2. **Einzel-Close ~5 min** unter MC3+Swap-Last kann am Head wieder auftreten (nur 1 pending) — dann normal **~1 Kerze** hinter CH, solange kein Rückstand akkumuliert.
3. **Frontend-Cache (P8/P9 B)** kann Anzeige zusätzlich verzögern.
4. **Swap voll** + BNB MC3 — weiterhin Ressourcenrisiko für CH/PD1-Latenz.

**Ressourcenschonender Betriebsmodus (Dokumentation only):** MC3-Worker-Throttling/Scheduling außerhalb DOGE-Line; BNB-Worker nicht gestoppt.

---

## 9. PASS / PARTIAL / BLOCKED

**PARTIAL**

- **PASS-Teile:** Ursache messbar; Throughput-Analyse; Monitoring ergänzt; Tail-Lag 0; keine Duplikate; Bounds-Parität.
- **Offen:** Live-Backlog-Batch nicht log-bewiesen; Canvas nicht verifiziert; kein isolierter Backlog-Recovery-Test.

---

## 10. STOP

Kein Commit, kein Push, keine Trading-/Order-Änderungen.  
**Ende DOGE-LIVE-P10.**
