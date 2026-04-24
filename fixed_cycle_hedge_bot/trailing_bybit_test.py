    import json
    import os
    import sys
    import time
    from pathlib import Path

    from dotenv import load_dotenv

    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT))

    env_path = ROOT / "env" / ".env.local"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv(ROOT / "env" / ".env")

    from fixed_cycle_hedge_bot.order_manager import BybitOrderManager

    CONFIG_PATH = "fixed_cycle_hedge_bot/config/fixed_cycle_config.json"
    API_KEY = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY")
    SECRET_KEY = os.getenv("BYBIT_API_SECRET") or os.getenv("SECRET_KEY")

    # Feste Test-Logik
    POSITION_IDX = 2                  # Hedge Mode SHORT side
    PARTIAL_CLOSE_PCT = 0.5           # 50% der Short-Position
    TRIGGER_DROP_PCT = 0.0023          # 0.5% unter Avg
    TRAILING_DISTANCE_PCT = 0.003     # 0.3% Distanz vom active_price
    TP_TRIGGER_BY = "MarkPrice"
    TP_ORDER_TYPE = "Limit"
    TP_LIMIT_PRICE_OFFSET_PCT = 0.0   # TP-Limit exakt auf TP-Trigger
    ENABLE_TRAILING_TEST = True
    POLL_INTERVAL_SEC = 1.0


    def load_config() -> dict:
        with open(CONFIG_PATH, encoding="utf-8") as config_fp:
            return json.load(config_fp)


    def fetch_all_open_positions(
        order_manager: BybitOrderManager,
        category: str,
        settle_coin: str,
    ) -> list[dict]:
        params = {
            "category": category,
            "settleCoin": settle_coin,
        }
        data = order_manager._get("/v5/position/list", params)
        if not isinstance(data, dict):
            raise SystemExit(f"Position list request failed: non-dict response: {data}")

        if data.get("retCode") != 0:
            raise SystemExit(f"Position list request failed: {data}")

        result = data.get("result") or {}
        positions = result.get("list") or []
        return positions


    def find_short_position_from_all_positions(
        positions: list[dict],
    ) -> tuple[str, float, str, float]:
        for position in positions:
            side = str(position.get("side") or position.get("positionSide") or "").lower()
            qty = float(position.get("size") or position.get("positionQty") or 0.0)
            symbol = str(position.get("symbol") or "").upper()

            if side in {"short", "sell"} and qty > 0 and symbol:
                avg_price = (
                    float(position.get("avgPrice") or position.get("entryPrice") or 0.0)
                    or float(position.get("averagePrice") or position.get("avg_entry_price") or 0.0)
                )
                return symbol, qty, side, avg_price

        raise SystemExit("No active short position found in open positions.")


    def set_partial_short_tp(
        order_manager: BybitOrderManager,
        symbol: str,
        category: str,
        qty: float,
        tp_trigger_price: float,
        tp_limit_price: float,
    ) -> dict:
        body = {
            "category": category,
            "symbol": symbol.upper(),
            "tpslMode": "Partial",
            "positionIdx": POSITION_IDX,
            "takeProfit": str(tp_trigger_price),
            "tpTriggerBy": TP_TRIGGER_BY,
            "tpOrderType": TP_ORDER_TYPE,
            "tpSize": str(qty),
            "tpLimitPrice": str(tp_limit_price),
        }
        print(f"[request] partial short tp body={body}")
        response = order_manager._post("/v5/position/trading-stop", json.dumps(body))
        print(f"[response] partial short tp response={response}")
        return response or {}


    def set_short_trailing_stop_full(
        order_manager: BybitOrderManager,
        symbol: str,
        category: str,
        active_price: float,
        trailing_distance: float,
    ) -> dict:
        body = {
            "category": category,
            "symbol": symbol.upper(),
            "tpslMode": "Full",
            "positionIdx": POSITION_IDX,
            "trailingStop": str(trailing_distance),
            "activePrice": str(active_price),
        }
        print(f"[request] short trailing stop full body={body}")
        response = order_manager._post("/v5/position/trading-stop", json.dumps(body))
        print(f"[response] short trailing stop full response={response}")
        return response or {}


    def main() -> None:
        if not API_KEY or not SECRET_KEY:
            raise SystemExit("Set BYBIT_API_KEY and BYBIT_API_SECRET before running the test.")

        config = load_config()
        category = config.get("category", "linear")
        settle_coin = config.get("settleCoin", "USDT")

        order_manager = BybitOrderManager(API_KEY, SECRET_KEY)

        positions = fetch_all_open_positions(
            order_manager=order_manager,
            category=category,
            settle_coin=settle_coin,
        )

        symbol, short_qty, short_side, short_avg_price = find_short_position_from_all_positions(positions)

        print(f"[info] found short position symbol={symbol} qty={short_qty} side={short_side} avg_price={short_avg_price}")

        if not (0 < PARTIAL_CLOSE_PCT <= 1):
            raise SystemExit("PARTIAL_CLOSE_PCT must be > 0 and <= 1.")
        if TRIGGER_DROP_PCT <= 0:
            raise SystemExit("TRIGGER_DROP_PCT must be > 0.")
        if TRAILING_DISTANCE_PCT <= 0:
            raise SystemExit("TRAILING_DISTANCE_PCT must be > 0.")

        partial_qty_raw = short_qty * PARTIAL_CLOSE_PCT
        partial_qty = order_manager.normalize_qty(symbol, partial_qty_raw, category)
        if partial_qty <= 0:
            raise SystemExit("Computed partial_qty is zero after normalization.")

        trigger_price_raw = short_avg_price * (1 - TRIGGER_DROP_PCT)
        trigger_price = order_manager.normalize_price(symbol, trigger_price_raw, category)

        tp_trigger_price_raw = trigger_price
        tp_limit_price_raw = tp_trigger_price_raw * (1 + TP_LIMIT_PRICE_OFFSET_PCT)
        tp_trigger_price = order_manager.normalize_price(symbol, tp_trigger_price_raw, category)
        tp_limit_price = order_manager.normalize_price(symbol, tp_limit_price_raw, category)

        active_price_raw = trigger_price
        active_price = order_manager.normalize_price(symbol, active_price_raw, category)

        # WICHTIG:
        # Trailing-Distanz wird vom active_price berechnet, nicht vom short_avg_price
        trailing_distance_raw = active_price * TRAILING_DISTANCE_PCT
        trailing_distance = order_manager.normalize_price(symbol, trailing_distance_raw, category)

        print(f"[info] computed partial_qty={partial_qty}")
        print(f"[info] computed trigger_price={trigger_price}")
        print(f"[info] computed tp_trigger_price={tp_trigger_price}")
        print(f"[info] computed tp_limit_price={tp_limit_price}")
        print(f"[info] computed active_price={active_price}")
        print(f"[info] computed trailing_distance={trailing_distance}")
        print(f"[info] polling every {POLL_INTERVAL_SEC} seconds until trigger is reached")

        try:
            while True:
                current_price = order_manager.fetch_last_price(symbol, category)
                if current_price is None:
                    print("[warn] failed to fetch current price, retrying...")
                    time.sleep(POLL_INTERVAL_SEC)
                    continue

                print(f"[watch] current_price={current_price} trigger_price={trigger_price}")

                if current_price > trigger_price:
                    time.sleep(POLL_INTERVAL_SEC)
                    continue

                print("[alert] trigger reached -> sending partial TP and trailing stop")

                tp_response = set_partial_short_tp(
                    order_manager=order_manager,
                    symbol=symbol,
                    category=category,
                    qty=partial_qty,
                    tp_trigger_price=tp_trigger_price,
                    tp_limit_price=tp_limit_price,
                )
                if not isinstance(tp_response, dict) or tp_response.get("retCode") != 0:
                    raise SystemExit(f"Partial short TP request failed: {tp_response}")

                if ENABLE_TRAILING_TEST:
                    trailing_response = set_short_trailing_stop_full(
                        order_manager=order_manager,
                        symbol=symbol,
                        category=category,
                        active_price=active_price,
                        trailing_distance=trailing_distance,
                    )
                    if not isinstance(trailing_response, dict) or trailing_response.get("retCode") != 0:
                        raise SystemExit(f"Short trailing stop request failed: {trailing_response}")

                print("[ok] trading-stop requests sent successfully")
                break

        except KeyboardInterrupt:
            print("[info] stopped by user before trigger was reached")
            return


    if __name__ == "__main__":
        main()