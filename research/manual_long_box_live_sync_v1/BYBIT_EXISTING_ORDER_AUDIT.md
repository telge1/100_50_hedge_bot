# BYBIT Order Execution Audit — Existing Bot Infrastructure (READ-ONLY)

**Date:** 2026-11-10  
**Scope:** Reuse audit for Manual Long Box live sync (no code changes, no live API calls in this audit).

**Primary SSOT for exchange code (this machine):**

`/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/vendor/core/bybit_order_manager.py`

Mirrored copy (same role, dashboard bundle):

`/home/telgenbuescher/projects/pools+ob+delta_bot/backtester/short/dashboard/vendor/core/bybit_order_manager.py`

Dashboard imports: `from core.bybit_order_manager import BybitOrderManager` and `from dashboard.vendor.core.bybit_order_manager import BybitOrderManager` (see `spread_recovery_hedge_short_dev/dashboard/app.py`).

**Related production-style execution (outside pools repo, import follow-up):**

- `burn_reentry_multiaccount/bots/follower_bot.py` — mirror absolute TP/SL + market size sync via same `BybitOrderManager`
- `spread_recovery_hedge_short_dev/dashboard/vendor/bots/shared/reentry_handler.py` — market reentry after TP
- Hedge dashboard orchestration: Master Bot API (`MASTER_BOT_API_URL`, default `http://localhost:8001`) for some hedge actions

**Market:** Bybit **V5**, **`category: linear`** USDT perpetual (not spot in audited paths).  
**Base URL:** `https://api.bybit.com` (mainnet in code; testnet not referenced in SSOT file).  
**Mode:** **Hedge** (`ensure_hedge_mode` → `position/switch-mode` mode=3), Long uses **`positionIdx: 1`**.

---

## A. Original code locations

| Function | Repository | File | Line (approx.) | Tatsächlich verwendet? |
|---|---|---|---:|---|
| Long-Limit-Order | spread_recovery | `dashboard/vendor/core/bybit_order_manager.py` | 1886 (`open_long_limit`) | **Definiert; kein Aufruf** in spread_recovery/burn_reentry (repo-wide `rg open_long_limit` → nur Definition) |
| Long-Market-Order | spread_recovery | `dashboard/vendor/core/bybit_order_manager.py` | 1805 (`open_long_market`) | **Ja** — z. B. `vendor/bots/shared/reentry_handler.py:231`, `burn_reentry_multiaccount/bots/follower_bot.py:238` |
| Stop-Loss (Long) | spread_recovery | `dashboard/vendor/core/bybit_order_manager.py` | 916 (`set_sl_long_order`) | **Ja** — z. B. `follower_bot.py:404`, `long_bot_org.py` (burn_reentry) |
| Take-Profit (Long) | spread_recovery | `dashboard/vendor/core/bybit_order_manager.py` | 226 (`set_tp_long_order`) | **Ja** — z. B. `follower_bot.py:390`, hedge/archive bots |
| Order-Amend (`/v5/order/amend`) | — | — | — | **NOT_IMPLEMENTED** (kein Treffer in SSOT `bybit_order_manager.py` oder auditierten Bots) |
| Cancel (single) | spread_recovery | `dashboard/vendor/core/bybit_order_manager.py` | 2539 (`cancel_order_direct`) | **Ja** — TP-Sync, `follower_bot`, `set_tp_long_order` cancel/replace |
| Open orders query | spread_recovery | `dashboard/vendor/core/bybit_order_manager.py` | 2910 (`fetch_open_orders_direct`) | **Ja** — Standard `orderFilter='StopOrder'`; Limit-Entry braucht **`orderFilter='Order'`** (Parameter existiert, Aufrufer-Pattern uneinheitlich) |
| Position (Long) | spread_recovery | `dashboard/vendor/core/bybit_order_manager.py` | 1562 (`get_long_position`) | **Ja** — follower, dashboard equity/hedge helpers |
| Hedge mode | spread_recovery | `dashboard/vendor/core/bybit_order_manager.py` | 45 (`ensure_hedge_mode`) | **Ja** — vor `open_long_*` |
| Tick/qty rounding | spread_recovery | `dashboard/vendor/core/bybit_order_manager.py` | 1896–1912 (`open_long_limit`) | **In Limit-Create implementiert** |
| Order link ID | spread_recovery | `dashboard/vendor/core/bybit_order_manager.py` | 1925–1926 | **Unterstützt** (`orderLinkId`); wenig Bot-Nutzung nachweisbar |
| Partial fill / position update | burn_reentry / hedge archives | WS + `handle_position_update` in legacy `long_hedgebot_be_old.py` | — | **Ja** in älteren Hedge-Bots; spread `vendor/bots` nur shared Module |

---

## B. Documented order flow (as implemented — not hypothetical)

### B1. Dominant production pattern (market entry + post-hoc TP/SL)

Typische Kette (nachweisbar z. B. Reentry + Follower):

```
Signal / Snapshot target
  → open_long_market(symbol, qty)     # POST /v5/order/create, Market, positionIdx=1
  → (optional settle wait)
  → get_long_position(symbol)       # fetch_positions_direct
  → set_tp_long_order(symbol, absolute_tp, size, position_idx=1)
  → set_sl_long_order(symbol, absolute_sl, size, position_idx=1)
```

**Referenzen:**

- Entry: `reentry_handler.place_stop_long_reentry` → `main_order_manager.open_long_market` (`reentry_handler.py:231`)
- TP/SL: `follower_bot._sync_orders` → `set_tp_long_order` / `set_sl_long_order` mit **absolutem `price` aus Snapshot** (`follower_bot.py:386–404`)

**Antworten auf Fragen 1–10:**

1. **Long-Limit:** `BybitOrderManager.open_long_limit` — **implementiert, produktiv nicht nachweisbar aufgerufen**.
2. **Long-Market:** `open_long_market` — **produktiv verwendet**.
3. **API-Body Limit (Auszug):** `category=linear`, `side=Buy`, `orderType=Limit`, `qty`, `price`, `positionIdx=1`, `timeInForce=GTC`, optional `orderLinkId` (Zeilen 1914–1926).
4. **Größe:** `qty` als Coins; Rundung über `get_symbol_info` → `qtyStep`/`min_qty`; Market-Pfad analog in `open_long_market`.
5. **SL/TP-Zeitpunkt:** **Nach** Positionsbestand (nicht im Entry-`order/create`-Body).
6. **SL/TP mit Entry?** **Nein** für Market/Limit-Create — separate **`/v5/position/trading-stop`** Calls.
7. **Bestätigung vor SL/TP:** Follower wartet auf Position (`size > 0`); Reentry verlässt sich auf WS/`handle_position_update` in Hedge-Bots (Kommentar in `reentry_handler.py:235`).
8. **Erfolgskontrolle SL/TP:** HTTP `retCode==0`; bei TP zusätzlich Duplikat-Prüfung + `fetch_open_orders_direct` Retries (`set_tp_long_order`, ab Zeile 251).
9. **API-Fehler:** Logging, `return None`/`False`; TP setzt bei vermutlich noch aktiver alter Order **keine** neue (`return None`, Zeilen 424–426).
10. **Teilfill Entry:** Kein dedizierter Limit-Teilfill-Handler für `open_long_limit`; Market-Flow nutzt Positionsgröße bei TP/SL. Follower: `PartiallyFilled` in `_is_open_status` für Stop-Orders, nicht für Entry-Limit-Amend.

### B2. Alternate path (dashboard hedge)

`app.py` nutzt `BybitOrderManager` direkt für Equity/Open-Orders-Anzeige und ruft teils **Master Bot API** (`/master/open-hedge`, `/master/set-tp`) statt lokal alle Schritte zu fahren. Für Manual Long Box muss geklärt werden, **welches Konto/Profil** (Main/Sub, Hedge) — außerhalb dieses Audits keine Live-Konfiguration gelesen.

### B3. Intended but unused path (limit entry)

`open_long_limit` ist vollständig implementiert (Hedge + Max-Leverage + Tick/Qty-Rounding). **Keine gefundene Bot-Aufrufkette** → für Manual Long Box **technisch verfügbar**, betrieblich **UNVERIFIED**.

---

## C. Protection mechanisms (existing)

| Mechanism | Evidence |
|-----------|----------|
| **Doppel-TP vermeiden** | `set_tp_long_order`: existierende TP scannen, exakter Preis → skip; sonst cancel + neu; bei Cancel-Fail + Order noch aktiv → **kein** neues TP |
| **Cancel Race** | `cancel_order_direct`: `110001` / `order not exists` / `too late to cancel` als OK (`2539–2618`) |
| **Hedge-Voraussetzung** | `open_long_market`/`open_long_limit` brechen ab wenn `ensure_hedge_mode` oder `ensure_max_leverage` fehlschlägt |
| **Reconcile open orders** | `fetch_open_orders_direct`, `get_long_position`; Follower vergleicht Desired vs Open mit Tick-Toleranz |
| **Idempotenz (Follower)** | Skip wenn `_matches_desired`; `max_actions_per_loop` |
| **Restart / params** | `order_params_manager.py` speichert TP/SL-Parameter JSON für manuelles Cancel-Recovery (Hedge-Bots) |
| **Manual Long Box Phase 1** | `sync_state.json` + `order_link_id` + Locks (eigenes Paket, DRY-RUN) |

**Offene Limit-Order dedupe:** Kein dediziertes „ein Entry-Link pro Symbol“-Pattern in `BybitOrderManager`; Follower synct über Snapshot-Seq, nicht über Chart-Drawing-ID.

---

## D. Reuse matrix (Manual Long Box Live Sync → original code)

| Need | Verdict | Original | Notes |
|------|---------|----------|-------|
| **A. Long limit @ manual entry** | `REUSE_DIRECT` | `open_long_limit` | Funktion fertig; **kein Prod-Aufruf** → Integration + Tests auf Mainnet-Creds später |
| **B. Exchange SL/TP @ absolute prices** | `REUSE_DIRECT` | `set_sl_long_order`, `set_tp_long_order` | Nur **nach Fill/Position**; Pattern wie `follower_bot._sync_orders` |
| **C. Amend open limit price** | `NOT_IMPLEMENTED` | — | Kein `/v5/order/amend`; praktikabler Vorlagen-Pfad: **`cancel_order_direct` + `open_long_limit`** (wie TP cancel/replace) — **noch nicht für Entry-Limit als fertige Funktion** |
| **D. Status + Drawing mapping** | `REUSE_WITH_ADAPTER` | `fetch_open_orders_direct` (`orderFilter='Order'`), `get_long_position`, `cancel_order_direct` | Default-Filter `StopOrder` listet **keine** normalen Limits; Mapping Drawing↔`orderLinkId` = Live-Sync State (Phase 1) |
| **E. Restart persistence** | `REUSE_WITH_ADAPTER` | `order_params_manager` (Hedge), `fetch_*_direct` reconcile | Box-Sync sollte State + Bybit-Realtime abgleichen (wie Follower) |
| **Entry size from notional** | `REUSE_WITH_ADAPTER` | `get_symbol_info`, rounding in `open_long_limit` | Notional→qty im Sync-Layer (Phase 1 DRY-RUN) |
| **Approach-from-above / Watcher** | `NOT_APPLICABLE` | — | Explizit out of scope für Live-Sync Phase 1 |
| **Master Bot API** | `UNVERIFIED` | `app.py` → `/master/*` | Ob Long-Box an API oder direkt an `BybitOrderManager` hängen soll, ist **nicht** im Code entschieden |

---

## E. Recommended minimal integration architecture (proposal only — not implemented)

1. **Service-Layer** behält `manual_long_box_live_sync_v1` (Activation, Drawing poll, `sync_state.json`).
2. **Execution-Layer** importiert **eine** `BybitOrderManager`-Instanz (gleiche Keys/Pfad wie Hedge-Main-Account — explizit konfigurieren).
3. **Create entry:** `ensure_hedge_mode` → `open_long_limit(symbol, entry_price, qty, order_link_id=f"mlb-sync-{drawing_id}")`.
4. **Move entry:** `cancel_order_direct(order_id, symbol, order_link_id=...)` → bei ACK/reconcile → `open_long_limit` mit neuem Preis (**kein** Amend bis `order/amend` im Manager existiert).
5. **Nach Fill (Position > 0):** einmalig oder bei SL/TP-Änderung: `get_long_position` → `set_tp_long_order` / `set_sl_long_order` mit Box-Preisen (Follower-Muster: cancel stale + set).
6. **Reconcile loop:** `fetch_open_orders_direct(symbol, order_filter='Order')` + `get_long_position`; vergleiche mit `sync_state.json`.
7. **Kein Auto-Trade** ohne `manual_long_box_activations.json`.

---

## F. Open risks

| Risk | Detail |
|------|--------|
| **Limit amend / chart drag** | Bybit amend nicht im Code; Cancel/Replace-Race mit Fill → braucht Muster aus `set_tp_long_order` + harte Reconcile |
| **Partial fill on limit** | Positionsgröße vs Order-Rest nicht in `open_long_limit`-Nutzern modelliert |
| **SL/TP only after fill** | Chart-TP/SL vor Fill sind nur „desired“ (wie Phase-1 Sync) |
| **fetch_open_orders default** | `StopOrder`-Default listet Entry-Limits nicht — falsche Reconcile wenn vergessen |
| **`open_long_limit` unproven in prod** | Regression-/Paper-Test nötig |
| **Multi-box** | Kein SSOT-Mechanismus; nur ein `order_link_id` pro Drawing geplant |
| **Hedge vs one-way** | Code setzt Hedge; falsches Konto-Setup → Orders rejected |
| **Master API vs direct OM** | Zwei Wege im Ökosystem; falsche Wahl → doppelte Infrastruktur |

---

## G. Comparison: `manual_long_box_live_sync_v1` (current)

| Phase-1 component | Maps to original |
|-------------------|------------------|
| `DryRunExchangeAdapter` | Placeholder for `BybitOrderManager` methods above |
| `order_link_id` | Matches `orderLinkId` in `open_long_limit` / `cancel_order_direct` |
| CREATE / AMEND (dry) | Live: CREATE=`open_long_limit`; AMEND=`NOT_IMPLEMENTED` → cancel+create |
| SL/TP sync | Document only today → live: `set_*_long_order` after fill |
| Drawing poll | Independent of dashboard; complements `persist_drawings` |

---

## H. Audit method & limits

- Static analysis + `rg` call graphs (no authenticated API calls).
- Secrets not read or logged.
- Legacy trees (`backup/`, `archive/`) cited only where they show **patterns** still reflected in SSOT `BybitOrderManager`.
- If production runs bots from paths outside audited repos (e.g. `live_bots/100_50_hedge_bot`), those scripts were **not** fully traced in this pass → mark integration points **UNVERIFIED** until verified on the deployment host.

---

**STOP — audit only. No implementation, no live orders.**
