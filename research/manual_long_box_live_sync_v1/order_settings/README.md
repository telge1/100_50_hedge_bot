# Manual Long Box → Bybit (Live Orders)

Long-Boxen aus den **spread research_charts** (`drawings.json`) werden über den Sync-Service auf Bybit gespiegelt: **Entry** als Limit, **SL/TP** aus der Box.

## Ablauf

1. **Long-Box** im Research-Chart zeichnen (`long_position` mit Entry, Stop, Target).
2. **Drawing-ID** aus `drawings.json` (Feld `drawing_id`, z. B. `d-…`) in  
   `research/manual_long_box_live_sync_v1/data/manual_long_box_activations.json` eintragen:
   ```json
   {
     "activations": {
       "d-DEINE-ID": {
         "enabled": true,
         "symbol": "DOGEUSDT",
         "notional_usd": 50,
         "dry_run": false,
         "note": "Erst dry_run:true testen, dann false für Live"
       }
     }
   }
   ```
3. **Credentials** in `order_settings/env/.env.local` (siehe `.env.example`).
4. **Scharfschalten:** `MANUAL_LONG_BOX_LIVE_ARMED=1` in `.env.local`.
5. **Größe:** feste Menge `MANUAL_LONG_BOX_ORDER_QTY` **oder** (Standard) `notional_usd / entry` aus der Activation (`MANUAL_LONG_BOX_USE_ACTIVATION_NOTIONAL=1`).
6. **Service starten:**
   ```bash
   cd /home/telgenbuescher/projects/pools+ob+delta_bot
   python3 research/manual_long_box_live_sync_v1/order_settings/run_live_sync_service.py
   ```
   Einmaliger Testlauf: `--once`. Erzwungen Dry-Run trotz ARMED: `--dry-run`.

## Sicherheit

- Ohne `MANUAL_LONG_BOX_LIVE_ARMED=1` nur DRY-RUN (keine HTTP-Writes).
- Pro Box nur mit expliziter Activation (`enabled` + passende `drawing_id`).
- Nach Neustart: **kein blindes Neuanlegen**, wenn lokaler State eine Order kennt, Bybit aber nicht (`reconcile_required`).

## Komponenten

| Datei | Rolle |
|-------|--------|
| `config.py` / `env/.env.local` | API-Keys, ARMED, Symbol, Qty |
| `credentials.py` | Kanonischer `BybitOrderManager` (spread vendor) |
| `exchange_adapter.py` | create/amend/cancel + REST-Reconcile |
| `order_sync.py` | `build_sync_engine()` für Live oder Dry |
| `run_live_sync_service.py` | Poller auf `drawings.json` |
| `run_connection_check_readonly.py` | Nur Lesen (Balance/Orders) |

Phase-1-Dry-Run bleibt: `research/manual_long_box_live_sync_v1/run_sync_service.py`.
