# BYBIT LIVE ORDER INTEGRATION V1 — Abschlussbericht

**Status:** Implementierung abgeschlossen, lokal getestet. **Keine echte Bybit-Order** in dieser Phase ausgeführt.

## 1. Verwendete Originaldateien und Funktionen

| Quelle | Verwendung |
|--------|------------|
| `spread_recovery_hedge_short_dev/dashboard/vendor/core/bybit_order_manager.py` | Einzige Order-API: `open_long_limit`, `amend_order_direct` (neu), `cancel_order_direct`, `fetch_open_orders_direct` |
| `research/manual_long_box_live_sync_v1/sync_engine.py` | Drawing → CREATE/AMEND (unveränderte Chart-Logik) |
| `research/manual_long_box_live_sync_v1/exchange_dry_run.py` | DRY-RUN-Referenz-Adapter |
| `research/manual_long_box_research_v1/recognition.py` | Long-Box-Erkennung |

Referenz-Audit: `BYBIT_EXISTING_ORDER_AUDIT.md`. WebSocket-Reuse: **nicht** angebunden; v1 nutzt **REST-Reconciliation** nach CREATE/AMEND (`reconcile.py`).

## 2. Neue Ordnerstruktur

```
research/manual_long_box_live_sync_v1/order_settings/
├── .env.example
├── .gitignore          # .env
├── README.md
├── config.py
├── credentials.py
├── exchange_adapter.py
├── order_sync.py
├── reconcile.py
├── tests/test_live_bybit_integration.py
└── BYBIT_LIVE_ORDER_INTEGRATION_REPORT.md
```

## 3. CREATE mit SL/TP

`BybitLiveExchangeAdapter.create_limit_long` ruft kanonisches `open_long_limit` auf mit:

- `order_link_id` = `mlb-sync-{drawing_id}`
- `stop_loss` / `take_profit` wenn `BYBIT_ATTACH_TPSL_ON_CREATE=1`
- `time_in_force`, `position_idx`, `category`, `tpsl_mode`, `tpOrderType`, `slOrderType`
- **Menge:** ausschließlich `MANUAL_LONG_BOX_ORDER_QTY` (ignoriert Notional aus Sync, Start-Sicherheit)

Nach HTTP-Ack: `wait_for_order_rest` via `fetch_open_orders_direct(..., order_filter="Order")`.

## 4. Native AMEND

`amend_order_direct` im kanonischen OrderManager (`POST /v5/order/amend`):

- Nur gesetzte Felder im Body
- Nie `takeProfit`/`stopLoss` mit `0` ohne expliziten Aufruf
- Tick-Rundung: Fix für `1e-05`-Darstellung bei Decimal-Länge (kleine Ergänzung nur in Amend-Helfer)

Sync-Engine:

- Entry-Änderung → `amend_limit` (nur Preis)
- Nur SL/TP geändert (Live) → `amend_limit_fields`

## 5. Änderungen am kanonischen BybitOrderManager

1. **`open_long_limit`** — rückwärtskompatible optionale kwargs: `stop_loss`, `take_profit`, `time_in_force`, `tpsl_mode`, `tp_order_type`, `sl_order_type`, `position_idx`, `category`
2. **`amend_order_direct`** — neue Methode
3. **Amend Tick-Decimals** — robustere `_tick_decimals` im Amend-Block

Kein zweiter OrderManager, keine parallele `session.place_order`-Schicht.

## 6. Rückwärtskompatibilität

- Bestehende `open_long_limit(symbol, price, size, order_link_id)`-Aufrufe unverändert (neue Parameter nur keyword-only mit Defaults)
- Phase-1 DRY-RUN-Service bleibt Default (`MANUAL_LONG_BOX_LIVE_ARMED=0`)
- `DryRunExchangeAdapter.create_limit_long` akzeptiert nun optionale `stop_loss`/`take_profit` (ignoriert)

## 7. WebSocket / REST-Reconciliation

| Schritt | Verhalten |
|---------|-----------|
| CREATE/AMEND HTTP `retCode=0` | Status `PENDING_ACK` bis REST-Bestätigung |
| `wait_for_order_rest` | Poll offene Orders, optional `expected_price` |
| Verbindungsverlust / Timeout | Kein `SYNCED`; Meldung `not_found_on_book` / `confirmed_price_pending` |
| WebSocket | Offen für v2 (`order.linear` aus bestehendem Stack) |

## 8. Drawing-ID ↔ Order-ID

- **order_link_id:** `mlb-sync-{drawing_id}` (persistent, idempotent)
- **exchange_order_id:** in `sync_state.json` (`StateStore` / `BoxSyncState`)
- Events in `sync_events.jsonl`

## 9. ENV-Variablen (später in `.env`)

Siehe `.env.example`:

- `BYBIT_API_KEY`, `BYBIT_API_SECRET`
- `BYBIT_ENVIRONMENT` (`mainnet` / `testnet` → setzt `base_url` in Factory)
- `BYBIT_CATEGORY`, `BYBIT_SYMBOL`, `BYBIT_POSITION_IDX`
- **`MANUAL_LONG_BOX_LIVE_ARMED=0`** (muss `1` sein für Writes)
- **`MANUAL_LONG_BOX_ORDER_QTY`** (Pflicht wenn armed)
- `BYBIT_LIMIT_TIME_IN_FORCE`, `BYBIT_ATTACH_TPSL_ON_CREATE`, `BYBIT_TPSL_*`

## 10. Tests

```text
research/manual_long_box_live_sync_v1/tests/           → 7 passed, 1 skipped
research/manual_long_box_live_sync_v1/order_settings/tests/ → 9 passed
(manual_long_box_research_v1/tests weiterhin grün bei Gesamtlauf)
```

Abgedeckt (mock, keine Exchange-Writes):

- Writes blocked ohne ARM/Credentials/Qty
- SL/TP an `open_long_limit`
- CREATE-Ack ohne Reconcile → `PENDING_ACK`
- Amend bei Filled abgelehnt
- Amend-Body nur `price` (kein SL/TP)
- Engine: kein Blind-Create nach Neustart bei stale state (Live)
- `build_sync_engine` Default dry_run

## 11. Offene Risiken

1. **`ensure_hedge_mode` / `ensure_max_leverage`** vor jeder Create — kann bei neuem Symbol API-Seiteneffekte haben
2. **SL/TP on create** — Kontomodus (Hedge/One-Way) und `positionIdx` müssen vor erstem Test verifiziert werden
3. **Teilfüllung** — Amend nur solange Order amendable; danach manuelle Positions-/TP-SL-Prüfung
4. **Kein WebSocket** — kurze Race zwischen Ack und Poll möglich
5. **Notional vs. ORDER_QTY** — Live ignoriert Activation-Notional zugunsten fester ENV-Menge (absichtlich)

## 12. Vor dem ersten realen Bybit-Test

1. `.env` aus `.env.example`, `chmod 600`, Keys eintragen
2. `MANUAL_LONG_BOX_LIVE_ARMED=1` nur für den **einmaligen** freigegebenen Test
3. `MANUAL_LONG_BOX_ORDER_QTY` — kleine DOGE-Menge, Nominal prüfen
4. Account: Mainnet/Testnet, Symbol DOGEUSDT linear, Hedge/One-Way, `BYBIT_POSITION_IDX`
5. Marktpreis, Entry **deutlich unter** Markt, PostOnly falls gewünscht
6. Konkurrierende Orders / offene Position prüfen
7. Checkliste §9 (User-Spec): CREATE → sichtbar → AMEND → SL/TP → CANCEL
8. **Kein** dauerhafter Sync-Service mit ARM=1 ohne Aufsicht

---

**STOP:** Implementierung und lokaler Testbericht fertig. Erste reale Testorder nur nach expliziter Freigabe mit konkreten Parametern.
