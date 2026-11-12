# BYBIT Mainnet — DOGEUSDT Long Testorder Vorbereitung (Phase 8)

**UTC:** 2026-11-11T08:13:08Z · **Modus:** READ-ONLY · **Keine Order gesendet**

---

## Parameter-Tabelle

| Parameter | Ergebnis |
|---|---|
| Bybit-Environment | **Mainnet** (`api.bybit.com`) |
| API-Authentifizierung | **OK** |
| Aktueller DOGEUSDT-Preis (Last) | **0.06807** |
| Bid / Ask | **0.06807 / 0.06808** |
| Kurszeit UTC | **2026-11-11T08:13:08Z** |
| Festgelegter Entry | **0.06731** (unverändert) |
| Vorgeschlagener SL | **0.06691** |
| Vorgeschlagener TP | **0.06801** |
| Erforderliche Mindestmenge DOGE | **75** (Qty-Step 1) |
| Orderwert USDT | **~5.05** |
| Tatsächlich eingestellter Hebel | **75×** (beide Slots idx 1/2) |
| Geprüfte Marginanforderung | **~0.07 USDT** Initial (5.05/75) |
| Aktuelle offene Positionen | **keine** (size 0, idx 1 & 2) |
| Aktuelle offene Orders | **0** |
| Position Index | **1** (Hedge-Long) |
| PostOnly gültig | **JA** (Entry **unter** Ask) |
| CREATE vorbereitet | **JA** (`open_long_limit` + SL/TP kwargs) |
| AMEND vorbereitet | **JA** (`amend_order_direct`, nur `price`) |
| CANCEL vorbereitet | **JA** (`cancel_order_direct`) |
| Live-Writes gesperrt | **JA** (`MANUAL_LONG_BOX_LIVE_ARMED=0`) |

---

## Beantwortung (1–7)

1. **Ist 0.06731 ein gültiger nicht-sofort-füllbarer Buy-Limit?**  
   **JA** zum Zeitpunkt der Abfrage: Entry **0.06731 < Ask 0.06808**. PostOnly sollte als Maker in die Book gehen, solange Ask darüber bleibt. **Keine Garantie** bei Kursverfall oder Spread-Engpass.

2. **Kleinste gültige DOGE-Menge?**  
   Min-Notional **5 USDT** → `ceil(5 / 0.06731) = 75` DOGE (Step **1**).

3. **Margin?**  
   Nominal **~5.05 USDT** bei **75×** → **~0.07 USDT** geschätzte Initial Margin. Verfügbar **~11.62 USDT** → ausreichend.

4. **SL/TP mit CREATE?**  
   **JA**, technisch vorbereitet (`stop_loss`, `take_profit`, `tpslMode=Full`, Market TP/SL). Geometrie: SL **unter**, TP **über** Entry; Distanzen **~−0.59 % / +1.04 %**. Keine offene Position → Full-TP/SL am CREATE gefährdet keine bestehende Absicherung.

5. **Order-Link-ID (Vorschlag einmaliger Test):**  
   `mlb-live-test-doge-001`  
   (Getrennt von `mlb-sync-{drawing_id}` der Box-Sync-Line.)

6. **Sicherheitsprobleme:**  
   - `open_long_limit` ruft beim **ersten** Live-CREATE ggf. `ensure_hedge_mode` / `ensure_max_leverage` auf (API-Schreibvorgänge außerhalb der Order selbst) — bekannt im kanonischen OM.  
   - Swap/Ressourcen irrelevant für diesen Test.  
   - Bei Fill vor AMEND/CANCEL: **keine** weiteren automatischen Schritte.  
   - API-Key/IP-Whitelist: aktuell **OK**.

7. **Freigabe-reif?**  
   **JA** für separaten einmaligen CREATE → RECONCILE → AMEND → RECONCILE → CANCEL, **nach** expliziter Nutzer-Freigabe und Setzen von `MANUAL_LONG_BOX_ORDER_QTY=75` (nur dann, nicht in `.env.local` ohne Freigabe eintragen).

---

## Geplanter CREATE-Payload (Referenz, nicht gesendet)

| Feld | Wert |
|---|---|
| category | linear |
| symbol | DOGEUSDT |
| side | Buy |
| orderType | Limit |
| price | **0.06731** |
| qty | **75** |
| timeInForce | PostOnly |
| positionIdx | 1 |
| stopLoss | 0.06691 |
| takeProfit | 0.06801 |
| tpslMode | Full |
| tpOrderType / slOrderType | Market |
| orderLinkId | mlb-live-test-doge-001 |

---

## Geplantes AMEND / CANCEL (noch nicht ausgeführt)

- **AMEND:** dieselbe `orderId` oder `orderLinkId`; nur `price` (z. B. **unter dann aktuellem Ask**, konkret vor Ausführung neu wählen).  
- **CANCEL:** `cancel_order_direct` + REST-Reconcile (`fetch_open_orders_direct` / `reconcile.py`).  
- Kein zweites CREATE; ACK ≠ bestätigt.

---

**STOP** — keine echte Order in dieser Phase.
