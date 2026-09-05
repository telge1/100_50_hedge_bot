# Source Purity Answers + Trade Event Import Plan

## HARD GATE STATUS

**`RESEARCH_TRADE_EVENTS_MISSING`** (for Golden / normal Fight windows)

Standard Fight-Trade-Events für abgedeckte Fenster sind **nicht** korrekt in
`btc_doge_research.research_public_trades` materialisiert. Der bisherige
„Lineage-Companion“ lädt **echte Fight-Events** aus
`orderbook_analysis.public_trades_canonical` — das ist **nicht** source-pure.

`mixed_sources_used=false` war technisch gesetzt, aber inhaltlich falsch, sobald
OB/OI/Liq aus `btc_doge_research` und Trades aus `orderbook_analysis` kamen.

---

## Explizite Antworten (Phase 1)

1. **Was ist „Lineage-Companion“?**  
   Fallback in `load_public_trades`: wenn `research_public_trades` das Fenster
   nicht überspannt, aber PUBLIC_TRADES-Tagesbatches READY/PARTIAL sind, werden
   Tick-Events aus `orderbook_analysis.public_trades_canonical FINAL` geladen.

2. **Nur Coverage-/Lineage-Metadaten?**  
   **Nein.**

3. **Werden Public-Trade-Events geladen?**  
   **Ja** — vollständige Trade-Zeilen (ts, trade_id, side, price, size, notional).

4. **DB/Tabelle?**  
   `orderbook_analysis.public_trades_canonical`

5. **Warum nicht immer `research_public_trades`?**  
   Full-History-Backfill schrieb primär **Buckets**
   (`research_public_trade_buckets_1s`), nicht Event-Rows. Event-Tabelle ist nur
   spärlich befüllt.

6. **Fehlen Events oder falsche Loader-Auswahl?**  
   Beides:
   - Für Golden `18:30–19:30Z`: **0** Rows in `research_public_trades`.
   - Es existieren 80738 BTC-Rows unter `16:30–17:30Z`, die **dieselben**
     `trade_id`/Preise wie OA `18:30–19:30Z` haben → **−2h Timestamp-Shift**
     (Pilot-/Import-Artefakt), nicht nutzbar als korrekte Fensterquelle.

7. **Wo Companion auftritt?**  
   BTC/DOGE immer dann, wenn Event-Span das angeforderte Fenster nicht deckt —
   praktisch Golden, DOGE Complete, und die meisten Session-Fenster.

8. **Row Counts / trade_id-Mengen?**  
   - OA Golden 18:30–19:30: 80738  
   - Research „16:30–17:30“: 80738, trade_ids = OA 18:30-Menge  
   - Research vs OA echte 16:30: Schnittmenge trade_id = **0**

9. **Companion vollständig entfernen?**  
   **Ja, aus dem Standardpfad** — sonst kein Source-Purity-READY.  
   Ohne Rematerialisierung: Controlled Gate `RESEARCH_TRADE_EVENTS_MISSING`.

10. **`mixed_sources_used=false` eingehalten?**  
    Manifest-Flag war `false`, aber **faktisch gemischt** (Research OB + OA Trades).
    Nach Fix: Companion default off; Flag muss Companion-Nutzung verbieten.

---

## Getrennter Importplan (nicht in dieser Phase ausführen)

**Ziel:** Event-materialisierte, zeitkorrekte `research_public_trades` für alle
READY/PARTIAL PUBLIC_TRADES-Tage BTC+DOGE.

1. Read-only Paritäts-Audit OA canonical ↔ buckets ↔ vorhandene Event-Rows  
2. DELETE/REPLACE nur der nachweislich shifted Pilot-Rows (eigenes Change-Ticket)  
3. Segmentweise INSERT aus kanonischer Source mit:
   - `event_time` = Source trade_ts (UTC)
   - stabile `trade_id` / `event_key`
   - `build_id`, `ingestion_batch_id`, `source_fingerprint`
4. Pro Segment: count, min/max ts, trade_id-set Parität vs Source  
5. Fight-CLI Golden ohne Companion erneut fahren  
6. Erst dann Source-Purity-READY prüfen

**Nicht:** stiller Companion, Raw-zstd-Replay, ungeprüfte Full-Table-Loads in dieser Phase.

**Optional später:** CH-Voraggregation (price_bin / TPO brackets) als
`PREAGGREGATION_REQUIRED`-Folgephase — erst nach korrekten Events oder aus
Buckets mit Paritätsnachweis.
