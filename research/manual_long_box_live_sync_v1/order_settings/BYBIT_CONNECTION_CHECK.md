# BYBIT Live Connection Check V1 (READ-ONLY)

**Datum (UTC):** 2026-11-11 ~08:01  
**Credentials-Datei:** `order_settings/env/.env.local`  
**Modus:** Keine CREATE/AMEND/CANCEL. `MANUAL_LONG_BOX_LIVE_ARMED` **nicht** gesetzt (`writes_allowed=false`).

---

## Ergebnistabelle

| Prüfung | Ergebnis |
|---|---|
| .env.local erkannt | **PASS** (Datei vorhanden) |
| Credentials korrekt geladen | **PASS** (Keys non-empty nach Parse; führende Leerzeichen/Quotes werden entfernt) |
| Dateirechte | **WARN** → auf **600** gesetzt (vorher 664) |
| Git ignore / nicht getrackt | **PASS** (`.gitignore` + root `.env*`) |
| Bybit-Authentifizierung | **FAIL** — `retCode=33004` `Your api key has expired.` (Mainnet wallet-balance) |
| Environment (technisch) | **Mainnet-URL** (`https://api.bybit.com`); private Auth **ungültig** (Key abgelaufen). Testnet-Probe: keine gültige Auth. **Kein Demo-Nachweis.** |
| DOGEUSDT-Marktdaten (öffentlich) | **PASS** — Status `Trading`, Last **~0.06811 USDT** @ 2026-11-11T08:01:19Z |
| Tick-Size | **0.00001** |
| Quantity-Step | **1.0** DOGE |
| Mindestmenge | **0** (lt. `lotSizeFilter`; `minNotionalValue` **5** USDT) |
| Aktuelle Position | **UNKNOWN** (private API blockiert) |
| Offene Orders | **0** (private Abfrage liefert leer/Fehler wegen Auth — keine verifizierten Orders) |
| Position Mode | **UNKNOWN** — private `/v5/position/list` → Key expired |
| Originaler OrderManager verbunden | **PASS** (Instanz aus `spread_recovery_hedge_short_dev/.../bybit_order_manager.py`, `require_armed=False`) |
| CREATE vorbereitet | **PASS** (Methode vorhanden; Writes gesperrt) |
| AMEND vorbereitet | **PASS** |
| CANCEL vorbereitet | **PASS** |
| Live-Writes weiterhin gesperrt | **PASS** |

---

## Konfigurationsanpassungen (minimal)

1. `config.py` lädt jetzt **`env/.env.local`** vor optionaler `order_settings/.env`.
2. Dotenv-Parser: trim + entfernt umgebende `"` an Werten.
3. `.gitignore` ergänzt um `env/.env.local`.
4. READ-ONLY-Helfer: `run_connection_check_readonly.py` (keine Secrets im JSON-Output).

**In `.env.local` gefundene Variablennamen (nur Namen):**  
`BYBIT_API_KEY`, `BYBIT_API_SECRET`  

**Optional für späteren Live-Test (fehlen noch in `.env.local`):**  
`BYBIT_ENVIRONMENT`, `BYBIT_CATEGORY`, `BYBIT_SYMBOL`, `BYBIT_POSITION_IDX`, `MANUAL_LONG_BOX_ORDER_QTY` — werden sonst aus Defaults / `.env.example` übernommen (`DOGEUSDT`, `mainnet`, `position_idx=1`, `ARMED=0`).

---

## Blocker vor erstem Live-Test

**Bybit API-Key ist abgelaufen.** Im Bybit-Account einen neuen Key erzeugen, in `.env.local` eintragen, Rechte für **Read + Trade** (linear) prüfen, erneut:

```bash
chmod 600 research/manual_long_box_live_sync_v1/order_settings/env/.env.local
PYTHONPATH=. python3 research/manual_long_box_live_sync_v1/order_settings/run_connection_check_readonly.py
```

Erst wenn `auth_wallet` = **OK**: Position Mode, Balance und offene Orders verlässlich.

---

## Testorder-Vorlage (nicht gesendet)

Alle Preise/Mengen nach **frischem** Markt und **gültiger** Auth nochmals prüfen. Basis: öffentlicher Last **~0.06811** (nur Stichprobe).

| Feld | Vorschlag |
|---|---|
| Symbol | DOGEUSDT linear |
| Side | Buy (Long) |
| Type | Limit, **PostOnly** (`BYBIT_LIMIT_TIME_IN_FORCE=PostOnly`) |
| Entry (unter Markt) | **0.06500** (~4.6 % unter Last-Stichprobe; tick-gerundet) |
| Menge | **80** DOGE (≥ minNotional 5 USDT; ~5.20 USDT @ 0.065) |
| Orderwert (ca.) | **~5.2 USDT** Nominal |
| SL | **0.06400** |
| TP | **0.07000** |
| `positionIdx` | **1** (Hedge-Long-Default im OrderManager — nach Auth mit `/v5/position/list` verifizieren) |
| Margin (Schätzung) | Bei ~10× Hebel grob **~0.5 USDT** Initial Margin (nur Näherung — `available` nach Auth lesen) |

**Hinweis:** Limit unter Markt ist **kein** Garantie gegen Fill. Erst Platzierung nach Freigabe + `MANUAL_LONG_BOX_LIVE_ARMED=1` + `MANUAL_LONG_BOX_ORDER_QTY`.

---

## STOP

Keine Orders gesendet. Keine Sync-Dienste im Write-Modus. Nächster Schritt: **API-Key erneuern**, Connection-Check wiederholen, dann Freigabe für einmaligen Live-Test.
