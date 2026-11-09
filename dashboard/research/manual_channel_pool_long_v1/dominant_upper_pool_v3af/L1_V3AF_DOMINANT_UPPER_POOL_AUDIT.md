# L1 V3AF — Dominante Upper-D2-Poolzone (READ-ONLY)

**Auftrag:** Isolierte Pool-Landschaftsanalyse. Keine TP-Implementierung, kein Backtest, keine Strategieänderung.

**Referenz:** DOGEUSDT 5m · Entry `2026-08-26 20:30 UTC` @ `0.08491` · Support `d-c0791ea0c65e`  
**Snapshot SHA256:** `b0718d82a94e8ad12d27d570c0cd7ce97cf8088a2db21c4c92830cd80fc600cb`  
**Pool-SSOT @ Entry:** `decision_close` = `2026-08-26 20:30:00 UTC` · Exact Unique-Origin D2, Gate depth ≥ 2, dist ≥ 0,5 %

**Methodik:** `merge_qualified_zones(merge_gap_pct=0.15)` + `enrich_zone_origins()` aus `tp_research_v3w/zone_features.py` (identisch V3W).

**Artefakte:** `l1_v3af_upper_zones.csv`, `l1_v3af_target_zone_details.json`

---

## 1. Zielbereich 0.08930–0.08963 — rekonstruierte Merge-Zone

Der Nutzerbereich liegt **innerhalb einer einzigen** V3W-Merge-Zone (keine Lücken zwischen qualifizierten D2-Segmenten; `max_gap_abs = 0`).

| Kennzahl | Wert |
|----------|------|
| **Unterkante (Merge)** | **0.08929** |
| **Oberkante (Merge)** | **0.089624** |
| Nutzerfenster | 0.08930 – 0.08963 (⊂ Merge-Spanne) |
| D2-Segmente (qualifiziert) | **7** |
| Segment-Tiefen (unten→oben) | 2, 3, 4, **5**, 4, 3, 2 |
| **Max Depth** | **5** |
| **Ø Depth** (breitengewichtet über Segmente) | **3.00** |
| **Unique ACTIVE Origins** (Micro-Overlap im Span) | **6** |
| Breite absolut | 0.000334 USDT |
| Breite % | **0.3741 %** |
| Abstand Entry → Unterkante | **5.1584 %** |
| Integrierte Density Σ(depth × Segmentbreite) | **0.001002** |
| Anteil Spanne mit Depth ≥3 | **58.08 %** |
| Anteil Depth ≥4 | **32.34 %** |
| Anteil Depth ≥5 | **9.58 %** |

**Exact-D2-Slices** innerhalb der Merge-Spanne (kanonische `D2Band`-Liste @ Entry, alle depth≥2 im SSOT-Fenster): siehe `density_slices_in_merge_span` in JSON — stufenförmige Depth 2→5 in der Mitte der Zone.

**Zusammenhängend vs. mehrere Zonen:** Eine **zusammenhängende** Merge-Zone (#6 von 15 oberhalb Entry). Nachgelagerte Zonen (z. B. 0.09073+) sind durch Gap **> 0,15 %** Entry getrennt.

---

## 2. Vergleichstabelle (alle Merge-Zonen @ Entry)

Sortierung nach Distanz. Vollständig: `l1_v3af_upper_zones.csv`. Auszug:

| dist-Rang | Unterkante | Oberkante | Breite % | Max D | Ø D | Origins | ∫Density | Dist % | Flags |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 0.08536 | 0.08546 | 0.117 | 4 | 3.40 | 4 | 0.00034 | 0.53 | Z08536 |
| 2 | 0.08604 | 0.08628 | 0.274 | 5 | 2.97 | 5 | 0.00070 | 1.33 | Z08604 |
| 3 | 0.08702 | … | 0.145 | 4 | … | 5 | … | 2.49 | |
| … | … | … | … | … | … | … | … | … | |
| **6** | **0.08929** | **0.089624** | **0.374** | **5** | **3.00** | **6** | **0.00100** | **5.16** | **TARGET** |
| 13 | 0.09342 | 0.09407 | 0.694 | 4 | … | **8** | 0.00134 | 10.02 | |
| 14 | 0.09436 | 0.09474 | 0.401 | **6** | 3.63 | 6 | **0.00137** | 11.13 | (V3AB-TP-Nähe) |

**15** Merge-Zonen oberhalb Entry (73 qualifizierte Einzelbänder).

---

## 3. Rankings (1 = bester Wert unter 15 Zonen)

| Kriterium | Rang TARGET (0.08929–0.089624) |
|-----------|-------------------------------|
| Größte zusammenhängende Breite (`width_abs`) | **3** |
| Höchste max. Density | **3** (geteilt; absolutes Maximum = 6 @ 0.09436) |
| Höchste Ø Density | **6** |
| Meiste Unique Origins | **2** (Maximum = 8 @ 0.09342) |
| Größte ∫Density | **3** (Maximum @ 0.09436) |
| Anteil Depth ≥4 an Spanne | **6** |

**Normalisierung (ohne neuen Score):**  
∫Density / Entry ≈ 1,18 × 10⁻⁵ (Preis-Einheiten); ∫Density / `width_abs` ≈ **3,0** (= Ø Depth über qualifizierte Segmente). Fairer Level-Vergleich: bandbreitengewichtete Depth — TARGET liegt hier **über** 0.08604-Zone (2,97), **unter** 0.09436-Zone (3,63).

---

## 4. Vergleich drei Fokus-Zonen

| | 0.08536–0.08546 | 0.08604–0.08628 | **0.08929–0.089624 (TARGET)** |
|--|-----------------|-----------------|--------------------------------|
| Dist % | 0.53 | 1.33 | **5.16** |
| Breite % | 0.12 | 0.27 | **0.37** |
| Max depth | 4 | 5 | 5 |
| Origins | 4 | 5 | **6** |
| ∫Density | 0.00034 | 0.00070 | **0.00100** |
| ∫Density-Rang (1–15) | 9 | 5 | **3** |
| Origins-Rang | 9 | 5 | **2** |

**Gegenüber den nahen TP2-Zonen ist TARGET objektiv stärker** (Breite, Origins, ∫Density). **Gegenüber allen Zonen ist sie stark, aber nicht absolut dominant** — ferne Cluster 0.093–0.095 übertreffen ∫Density und teils max depth (6) bzw. Origins (8).

---

## 5. Historische Kausalität (nur @ Entry vs. @ Touch)

| Zeitpunkt | `decision_close` |
|-----------|------------------|
| Entry | 2026-08-26 20:30 UTC |
| Erster Touch Unterkante (V3AC) | 2026-08-27 08:45 UTC → Close **08:50 UTC** |

- **Origins im Nutzerfenster 0.08930–0.08963 @ Entry:** 6 (siehe JSON).
- **@ Touch (08:50 UTC):** dieselben **6** Origins — **keine** neuen, **keine** verlorenen Origin-IDs in diesem Fenster.
- **Merge-Zone @ Touch:** weiterhin **0.08929–0.089624**, max_depth **5**, origins **6** (unverändert).
- Keine nachträgliche „Erfindung“ der Zone: Struktur für TP-Entscheidung @ Entry vollständig verfügbar.

*(Hinweis: Katalog-Snapshot ist instantan ACTIVE @ jeweiliger `decision_close`; Änderungen jenseits Origin-Sets in diesem Fenster sind hier nicht feststellbar — SSOT liefert dieselbe Merge-Geometrie.)*

---

## 6. TP-Auswahl — Konzept (keine Implementierung)

### 6.1 Ist TARGET dominanter als nahe Zonen?

**Ja** — auf Breite, Unique Origins (#2 gesamt), ∫Density (#3 gesamt), sichtbare Depth-5-Kernzone.

### 6.2 Dominant gegenüber allen Upper-Zonen?

**Teilweise.** Top bei Origins (Rang 2), Top-3 bei Breite/∫Density/max depth (5). **Nicht** Top bei: max depth absolut (6 @ 0.09436), Origins (8 @ 0.09342), ∫Density (Rang 1 @ 0.09436 = V3AB-Fern-TP-Umfeld).

### 6.3 Welche V3W-Methode erfasst das?

| Methode | Verhalten für diese Zone |
|---------|---------------------------|
| `merge_qualified_zones` + `enrich_zone_origins` | Bildet TARGET exakt ab |
| **TP2** (`resolve_tp2`) | **Erste** Zone, die Schwellen erfüllt → **0.08536**, nicht TARGET |
| **TP3a** max depth | Wählt eher **0.09436** (depth 6) |
| **TP3b** max origins | Wählt **0.09342** (8 Origins) |
| **TP3c** depth×width | Eher ferne breite Cluster |

### 6.4 Warum nicht TP2a/b/c?

TP2 scannt **aufsteigend** und stoppt bei der **ersten** „substantiellen“ Zone. 0.08536 und 0.08604 erfüllen TP2a/b **vor** TARGET — designbedingt, nicht wegen fehlender Daten.

### 6.5 Mögliche Auswahlprinzipien (nur Dokumentation)

1. **Mindest-Distanz-Filter** vor Substanzwahl (V3E-Philosophie; vermeidet 0.08536, wählt ggf. TARGET).
2. **Beste Substanz unterhalb Kanaldeckel / innerhalb X %** (max ∫Density oder Origins, nicht erste).
3. **TP3-Variante** mit Distanz-Cap (z. B. max origins nur innerhalb 6 % Entry).
4. **Zweite/N-te substanzielle Zone** (M3-ähnlich, aber Index > 0).
5. **Kombination Entry-Gate** (`pool_landscape` substantial) + **anderer TP-Selektor** — getrennt halten.

### 6.6 Risiken

- Frühere Mindest-Distanz → verpasste legitime Nahe-TPs in anderen Monaten.
- Reine max-∫Density / max-origins → wieder Fern-TP wie V3AB (0.09496).
- Dominanz-Regel nur auf einen visuellen Band → Overfit (hier vermieden: nur vorgegebene V3W-Schwellen ausgewertet).

---

## 7. Wiederverwendbare Codekomponenten

| Komponente | Pfad |
|------------|------|
| Zonen-Merge + Origins | `tp_research_v3w/zone_features.py` |
| TP2/TP3-Logik | `tp_research_v3w/tp_targets.py` |
| D2 @ decision_close | `pool_d2_metrics.analyze_d2_landscape`, `pd1_exact_d2_adapter` |
| Kontext/Snapshot | `support_break_exit_v3ab/v3ab_context.build_context_from_snapshot` |

---

## 8. Kurzantwort (Pflichtausgabe)

| Frage | Antwort |
|-------|---------|
| **Exakte Kennzahlen 0.08930–0.08963** | Merge **0.08929–0.089624**, 7 Segmente, depth 2–5 (max **5**), **6** Origins, Breite **0.374 %**, ∫D **0.001002**, Dist **5.16 %** |
| **Vs. 0.08536 / 0.08604** | TARGET: höhere Breite, mehr Origins, höhere ∫Density; nahe Zonen schwächer (Rang ∫D 9 bzw. 5) |
| **Rang TARGET (6 Kriterien)** | Breite **3**, max D **3**, Ø D **6**, Origins **2**, ∫D **3**, Anteil ≥4 **6** |
| **Dominierende Konzentration?** | **Ja** vs. nahe TP2-Zonen; **mittel bis stark** im Gesamtfeld; **nein** vs. Fern-Cluster 0.093–0.095 (V3AB-TP-Höhe) |
| **Code-Wiederverwendung** | V3W `merge_qualified_zones`, `enrich_zone_origins`, optional `resolve_tp3` / erweiterte Auswahl mit Mindest-Distanz — **ohne** neue Density-Engine |

---

**STOP** — READ-ONLY abgeschlossen.
