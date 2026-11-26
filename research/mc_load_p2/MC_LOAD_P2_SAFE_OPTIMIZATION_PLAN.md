# MC-LOAD-P2 Safe Optimization Plan (Design Only)

**Keine Implementierung in P2.** Vergleicht drei Maßnahmen nach Nutzen, Komplexität, Regressionsrisiko — **ohne erfundene Performance-Zahlen**.

Constraints: kanonische Candle-/EMA-Module, keine neue Aggregation, keine stillen History-Kürzungen, keine Trading/PD1/OPI/Multi-Chart-Änderungen.

---

## Zielbild (Zwei Phasen)

| Phase | User-visible | Data correctness |
|-------|--------------|------------------|
| **A** | `setData` mit Viewportfenster + definierter Warmup-Klammer (EMA/Stoch identisch über `pane_bundle`) | EMA-Werte = volle Historie auf demselben TRP-Pfad, nur Request-Fenster kleiner |
| **B** | Hintergrund-`loadPane` / History-Merge für volle `lldHistoryWeeks` | Kein Überschreiben mit unvollen EMA; optional `preserveView: true` |

Phase B **muss** weiterhin volle Weeks liefern, wenn der User scrollt — kein silent cap.

---

## Maßnahme 1 — `loading…` nach erstem erfolgreichen `setData` freigeben

**Idea:** `releasePaneLoadOwner` (oder äquivalent) direkt nach `applyPaneBundle` mit Kerzen; `ensurePaneCaughtUp` + `ema-post-catch-up` als `void` im Hintergrund mit bestehendem Gen/Owner-Guards.

| Dimension | Rating |
|-----------|--------|
| **Nutzen** | Hoch für **wahrgenommene** Bereitschaft; ändert keine Datenmenge |
| **Komplexität** | Niedrig–mittel (Owner-/Supersede-Logik sorgfältig) |
| **Regressionsrisiko** | Mittel: kurzes Fenster mit veraltetem Tip oder EMA; Status-Text vs. tatsächlicher Catch-up |

**Sicherheit:** Keine falschen EMA, solange Linien erst nach Catch-up/Reload gezeichnet werden oder stale visuell markiert bleiben.

---

## Maßnahme 2 — Initiales Pane-Load auf Viewport + Warmup begrenzen (Phase A)

**Idea:** Erster `POST /api/research/pane` mit `from`/`to` oder `limit` ≈ `visibleBars + warm + margin` (z. B. 120 + 220 + Puffer), volle Weeks späterer Request wie heute `historyLoadLimitForTimeframe`.

**Code-Anker:** bestehend `pane_bundle` Warmup bei `start` gesetzt (`_max_indicator_warmup_bars`, `load_start` shift); Client `navPaneLoadRangeForFreeNav` / GO-TO-Muster wiederverwenden — **kein** neuer Aggregator.

| Dimension | Rating |
|-----------|--------|
| **Nutzen** | Hoch — weniger JSON, kürzerer `setData` (qualitativ, nicht quantifiziert hier) |
| **Komplexität** | Mittel–hoch (zwei Load-Generationen, Scroll-Back Prefetch, History-Hint) |
| **Regressionsrisiko** | Mittel–hoch: Scroll über nicht geladene Vergangenheit, Race zwischen Phase A/B |

**Sicherheit:** Nur sicher wenn Phase-B EMA auf **identischer** Server-Pipeline nachzieht und Chart EMA erst nach B finalisiert oder Phase-A-EMA strikt Teilmenge mit gleichen Werten auf Überlappung ist.

---

## Maßnahme 3 — `paneEmaStaleVsCandles` / ema-post-catch-up entdoppeln

**Idea:** Nach initialem `pane-load` mit vollständiger Server-EMA: stale-Check mit Granularität (per line, forming bar policy) oder Skip wenn `packed.ema` Tip innerhalb 1 Bar; vermeidet **50k** Zweit-POST bei False Positive.

| Dimension | Rating |
|-----------|--------|
| **Nutzen** | Hoch **wenn** False Positive (Trace legt nahe: sofort nach 66k-Bundle) |
| **Komplexität** | Niedrig–mittel (Diagnostik + Bedingungsverschärfung) |
| **Regressionsrisiko** | Niedrig–mittel: echtes Stale bleibt wenn forming/live edge |

**Sicherheit:** Keine EMA-Wertänderung — nur redundante Requests vermeiden.

---

## Vergleichsmatrix

| Maßnahme | Erwarteter Nutzen (qualitativ) | Technische Komplexität | Regressionsrisiko |
|----------|-------------------------------|------------------------|-------------------|
| **1** Entkopple loading | Wahrnehmung ++ | Niedrig–mittel | Mittel |
| **2** Viewport-first load | setData/Netz ++ | Hoch | Mittel–hoch |
| **3** EMA reload dedupe | Netz/CPU ++ bei Stale-FP | Niedrig–mittel | Niedrig–mittel |

**Empfohlene Reihenfolge (nur Planung):** **3** → **1** → **2** (steigender Scope).

---

## Diagnose-Reparatur (Voraussetzung für P3-Messung)

Vor jeder Optimierung: **http_id-scoped** Events (siehe `MC_LOAD_P2_ROOT_CAUSE.md` §7), sonst sind setData- und Candle-Latenzen im JSON nicht verlässlich.

---

## Nicht in Scope

- Pool-Window 11–17 s, Lease 409, LLD in `pane_bundle`
- Parallele PD1 `refreshPane_coalesced`
- Reduktion `MAX_LIMIT` / History Weeks ohne explizite Produktentscheidung

---

## Schlussantwort (Planung)

**Warum braucht der Chart „lange“?**  
Weil die **erste** Antwort absichtlich **Monate** 5m-Kerzen enthält und der Browser sie **alle** in LWC lädt, während die UI nur **~120** Bars zeigt; der Initialpfad wartet zusätzlich auf **Gap-Backfill** und oft **EMA-Nachladen**.

**Frühere erste Kerzen ohne Genauigkeitsverlust:**  
**Maßnahme 2 (Viewport + Warmup, Phase B volle Weeks)** plus **Maßnahme 3**; **Maßnahme 1** für „loading…“ ohne Datenkompromiss.

Der Trace 26.11. meldet **`loading_indicator_hidden` bei t_rel_ms ≈ 42478** (**~42,5 Sekunden**) — Optimierungen zielen auf **Long-Tasks** (setData 67k), **serielle Catch-up/EMA-Gates** und **Follow-up** (50k EMA). Frühere P2-Textstellen mit „~42 ms“ waren ein **Einheitenfehler** (ms vs. s seit Trace-Start).

**STOP**