# Watchdog & Risk Control

## Included scripts

- `safety_order_watchdog.py`: Prüft fehlende Schutzorders und ruft `shared_scripts/stop_with_cleanup.sh` auf, wenn eine Ordergruppe fehlt.
- `wallet_refill_watchdog.py`: Überwacht Start-Wallets und erkennt, ob die Futures-Wallet auf ≤50 % fällt (`would_refill`/`would_cashout`), Phase 1 ist passiv ohne echte Transfers.
- `wallet_transfer_executor.py`: Optionales Script für echte Universal-Transfers zwischen Main Funding und Subaccount-Wallets (Refill/Cashout).

## Beispielbefehle

```bash
python3 live_bots/100_50_hedge_bot/watchdog/safety_order_watchdog.py --loop --interval 10
python3 live_bots/100_50_hedge_bot/watchdog/wallet_refill_watchdog.py --loop --interval 30 --dry-run
python3 live_bots/100_50_hedge_bot/watchdog/wallet_refill_watchdog.py --capture-start-wallet --bot-name long_bot_1
```
## Konfigurationsbeispiel

```yaml
Main_bot:
  api_key: "${MAIN_API}"
  secret_key: "${MAIN_SECRET}"
  uid: "MAIN_UID"

Long_bot_1:
  api_key: "${LONG1_API}"
  secret_key: "${LONG1_SECRET}"
  sub_uid: "549637342"
```

Short_bot_1, Short_bot_2, etc. folgen dem selben Pattern. Der Executor sucht case-insensitive nach `Main_bot`/`main_account` und `Long_bot_1` (oder `long_bot_1`), damit die reale `config.yaml`-Struktur korrekt abgebildet ist.

```bash
python3 live_bots/100_50_hedge_bot/watchdog/wallet_transfer_executor.py \
  --bot-name long_bot_1 \
  --direction refill \
  --amount 1 \
  --coin USDT \
  --dry-run
```

```bash
python3 live_bots/100_50_hedge_bot/watchdog/wallet_transfer_executor.py \
  --bot-name long_bot_1 \
  --direction cashout \
  --amount 1 \
  --coin USDT \
  --dry-run
```

```bash
python3 live_bots/100_50_hedge_bot/watchdog/wallet_transfer_executor.py \
  --bot-name long_bot_1 \
  --direction refill \
  --amount 1 \
  --coin USDT \
  --max-transfer-usdt 1
```

Echte Transfers nur durchführen, wenn `main_account.uid` / `long_bot_X.sub_uid` korrekt konfiguriert sind und ausreichend USDT vorhanden ist.
