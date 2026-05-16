# Watchdog & Risk Control

## Included scripts

- `safety_order_watchdog.py`: Prüft fehlende Schutzorders und ruft `shared_scripts/stop_with_cleanup.sh` auf, wenn eine Ordergruppe fehlt.
- `wallet_refill_watchdog.py`: Überwacht Start-Wallets und erkennt, ob die Futures-Wallet auf ≤50 % fällt (`would_refill`/`would_cashout`), Phase 1 ist passiv ohne echte Transfers.

## Beispielbefehle

```bash
python3 live_bots/100_50_hedge_bot/watchdog/safety_order_watchdog.py --loop --interval 10
python3 live_bots/100_50_hedge_bot/watchdog/wallet_refill_watchdog.py --loop --interval 30 --dry-run
python3 live_bots/100_50_hedge_bot/watchdog/wallet_refill_watchdog.py --capture-start-wallet --bot-name long_bot_1
```
