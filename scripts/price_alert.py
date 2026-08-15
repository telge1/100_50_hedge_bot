#!/usr/bin/env python3
"""
Preis-Alarm: Benachrichtigt (ntfy), sobald ein Symbol einen Zielpreis erreicht.

Verwendet die Bybit Market Tickers API (öffentlich, keine API-Keys nötig):
  GET /v5/market/tickers?category=linear&symbol=TONUSDT

Beispiele:
  python scripts/price_alert.py --symbol TONUSDT --target-price 7.50 --trigger above
  python scripts/price_alert.py --symbol BTCUSDT --target-price 95000 --trigger below
"""
import argparse
import atexit
import json
import os
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

STATE_FILE = PROJECT_ROOT / "data" / "state" / "price_alert.json"
_STATE_CLEANUP_REGISTERED = False

BYBIT_BASE_URL = "https://api.bybit.com"


def _load_state() -> dict:
    """Lädt State-Datei für aktive Price-Alerts."""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(data: dict) -> None:
    """Speichert State-Datei."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print("[price_alert] State schreiben fehlgeschlagen: %s" % e, file=sys.stderr)


def _alert_key(symbol: str, target_price: float, trigger: str) -> str:
    """Eindeutiger Key für mehrere Alerts pro Symbol."""
    return f"{symbol}_{target_price:.6f}_{trigger}"


def _remove_state_entry(alert_key: str) -> None:
    """Entfernt Eintrag aus der State-Datei."""
    data = _load_state()
    data.pop(alert_key, None)
    if data:
        _write_state(data)
    elif STATE_FILE.exists():
        try:
            STATE_FILE.unlink()
        except Exception:
            pass


def fetch_current_price(symbol: str, timeout: int = 5) -> float | None:
    """
    Holt den aktuellen Preis von Bybit Market Tickers API (öffentlich, keine Auth).
    category=linear für USDT Perpetual.
    """
    try:
        import urllib.request
        symbol_upper = symbol.strip().upper()
        url = f"{BYBIT_BASE_URL}/v5/market/tickers?category=linear&symbol={symbol_upper}"
        req = urllib.request.Request(url, headers={"User-Agent": "HedgeBot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        result = data.get("result") or {}
        items = result.get("list") or []
        if not items:
            return None
        last_price = items[0].get("lastPrice")
        if last_price is None:
            return None
        return float(last_price)
    except Exception as e:
        print("[price_alert] Preis-Abfrage fehlgeschlagen: %s" % e, file=sys.stderr)
        return None


def condition_met(current: float, target: float, trigger: str) -> bool:
    """True wenn die Alarm-Bedingung erfüllt ist."""
    if trigger == "above":
        return current >= target
    if trigger == "below":
        return current <= target
    return False


def send_alert(symbol: str, target_price: float, trigger: str, current_price: float) -> bool:
    """Sendet ntfy-Benachrichtigung und spielt lokalen Laptop-Ton."""
    ntfy_ok = False
    sound_ok = False
    try:
        from dashboard.utils.notifications import send_ntfy_alert, play_alert_sound
        msg = f"{symbol}: Preis {current_price} {'≥' if trigger == 'above' else '≤'} {target_price}"
        title = "Price Alert"
        tags = ["chart_with_upwards_trend"] if trigger == "above" else ["chart_with_downwards_trend"]
        ntfy_ok = send_ntfy_alert(message=msg, title=title, priority="high", tags=tags)
        sound_ok = play_alert_sound(repeats=2)
    except Exception as e:
        print("[price_alert] Alert-Ausgabe fehlgeschlagen: %s" % e, file=sys.stderr)
    return ntfy_ok or sound_ok


def main():
    parser = argparse.ArgumentParser(description="Preis-Alarm: Benachrichtigung wenn Symbol Zielpreis erreicht.")
    parser.add_argument("--symbol", required=True, type=str, help="Trading-Symbol (z.B. TONUSDT)")
    parser.add_argument("--target-price", required=True, type=float, help="Zielpreis")
    parser.add_argument(
        "--trigger",
        choices=["above", "below"],
        default="above",
        help="Alarm wenn Preis >= Ziel (above) oder <= Ziel (below). Default: above",
    )
    parser.add_argument("--poll-interval", type=float, default=10.0, help="Sekunden zwischen Preis-Abfragen (Default: 10)")
    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    target = args.target_price
    trigger = args.trigger
    poll_interval = max(1.0, args.poll_interval)

    alert_key = _alert_key(symbol, target, trigger)

    def cleanup():
        _remove_state_entry(alert_key)
        print("[price_alert] Beendet für %s." % symbol, file=sys.stderr)

    def signal_handler(signum, frame):
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    atexit.register(cleanup)

    # State: mit bestehenden Alerts mergen (Dashboard-API schreibt beim Start, Script ergänzt falls nötig)
    existing = _load_state()
    existing[alert_key] = {
        "symbol": symbol,
        "pid": os.getpid(),
        "target_price": target,
        "trigger": trigger,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }
    _write_state(existing)

    print("[price_alert] %s: Alarm wenn Preis %s %s (Poll alle %ss)" % (symbol, trigger, target, poll_interval))

    while True:
        price = fetch_current_price(symbol)
        if price is not None:
            if condition_met(price, target, trigger):
                print("[price_alert] ALARM: %s = %s (%s %s)" % (symbol, price, trigger, target))
                ok = send_alert(symbol, target, trigger, price)
                print(
                    "[price_alert] Alert-Ausgabe %s (ntfy und/oder Laptop-Ton)."
                    % ("ok" if ok else "fehlgeschlagen"),
                    file=sys.stderr,
                )
                cleanup()
                break
            print("[price_alert] %s aktuell: %s (warten auf %s %s)" % (symbol, price, trigger, target))
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
