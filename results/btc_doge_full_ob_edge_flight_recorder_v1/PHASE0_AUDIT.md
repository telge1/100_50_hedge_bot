# Phase 0 — Full-OB Bestandsaudit

**Datum:** 2026-09-03  
**Zielrepo (Arbeitsbaum):** `spread_recovery_hedge_short_dev` @ `feature/btc-doge-research-db`  
**Collector-Host:** `orderbook_analyse` (Sibling, Shared WS)

## Verdict vor Änderung

`FULL_OB_SYNC_CONTRACT_BLOCKED_REPAIR_REQUIRED` — der bestehende Full-OB-Pfad puffert Deltas und holt REST, erfüllt aber den Bybit-`seq`/`u`-Abgleich **nicht**. Flight-Recorder erst nach Sync-Repair.

## 1. Komponenten

| Pfad | Rolle |
|---|---|
| `orderbook_analyse/.../full_book_state.py` | `FullBookState`, `apply_snapshot`/`apply_delta`, UI-Aggregation |
| `orderbook_analyse/.../on_demand_full.py` | `FullBookOnDemandManager` — Lease, REST, WS-Ingest |
| `orderbook_analyse/.../collector.py` | Shared Bybit-WS, Dispatch `depth=0` |
| `orderbook_analyse/.../on_demand_socket.py` | Unix-Socket Control |
| `spread_recovery.../dashboard/research_charts/ob1000_on_demand.py` | Bridge `depth=0` → Full |
| `.../api.py`, `research_charts.js` | Lease/HB/Snapshot für Charts |

Topic: `orderbook.full.{symbol}` — **nicht** `orderbook.1000.*`.

## 2–5. Lifecycle / Isolation / WS

- Charts starten Full-OB über Lease `acquire`/`heartbeat`/`release` mit `depth=0`.
- An Browser-Tab gebunden (sessionStorage `lease_id`, 15s HB, 45s TTL). Ohne Tab → Unsubscribe nach Grace.
- BTC/DOGE isoliert (eigene `FullBookRuntime`/`FullBookState`), Cap 2 Topics.
- **Eine** gemeinsame Public-WS im Collector (OB200 + OB1000 + Full).

## 6–7. Raw-Payload

Erstankunft: `Collector._handle_raw` → `handle_orderbook_message` → `FullBookOnDemandManager.handle_message`.

| Feld | Am Callback | Nach Apply persistiert |
|---|---|---|
| topic/type/ts | ja | nein (nur Routing/ts→event_ts) |
| cts | oft im WS | **nicht gelesen** |
| data.s/b/a/u/seq | ja | b/a/u/seq im Book; s ignoriert |
| local receive | `received_at` | **nicht gespeichert** |

## 8. Sync heute (Lücke)

Aktuell: Buffer → REST → Deltas mit `u > snap.u` anwenden.  
Fehlt laut Bybit Full-OB Docs:

- `u`-Kontinuität im Buffer (Clear bei Gap)
- sinkendes `seq` verwerfen
- Snapshot-Refetch wenn `snap.seq < first.seq`
- Match `snap.seq`/`snap.u` gegen Delta; bei Seq-gleich/`u`-Mismatch refetch
- Live: `u == local+1`; Gap → Discard+Resync; `u=1` → Resync
- Stale `u < local` **ignorieren** (heute fälschlich Resync)

## 9–12. Rest

- Reconnect: `on_reconnect` → resync_needed → tick resubscribe+REST.
- Kein Crossed-Book-Check; pending Cap 5000→2000 (stiller Truncate).
- RAM-only; Health `full_book_*`.
- Tests: State/Aggregation/Stale; **keine** Sync-Contract-Tests für Manager.

## Hook (ohne 2. WS / 2. Book)

`FullBookOnDemandManager.handle_message` — Raw-Payload + `received_at` vor/nach Apply; gleicher `FullBookState` für Chart + Recorder.

## RPI

Bybit: RPI **nicht** in Full-OB REST/WS. Im Repo nur Public-Trade-RPI; Full-OB Coverage: `rpi_included=false`.

## Nächster Schritt

1. Sync-Contract reparieren + Tests.  
2. Kleinstmögliche Flight-Recorder-Erweiterung (Keeper-Lease, Ringbuffer, Event-Capture).  
3. Kein Restart ohne Freigabe.
