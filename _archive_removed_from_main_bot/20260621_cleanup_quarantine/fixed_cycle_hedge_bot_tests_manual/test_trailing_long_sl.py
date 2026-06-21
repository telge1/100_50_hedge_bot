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
from fixed_cycle_hedge_bot.trailing_fallback import TrailingFallbackManager

CONFIG_PATH = "fixed_cycle_hedge_bot/config/fixed_cycle_config.json"
API_KEY = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY")
SECRET_KEY = os.getenv("BYBIT_API_SECRET") or os.getenv("SECRET_KEY")

ACTIVATION_DROP_PCT = 0.003
TRAILING_OFFSET_PCT = 0.001
REQUOTE_STEP_PCT = 0.002
SAFE_OFFSET_PCT = 0.0015
CANCEL_TIMEOUT_SEC = 5.0
POLL_INTERVAL_SEC = 0.2


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as config_fp:
        return json.load(config_fp)


def find_long_position(order_manager: BybitOrderManager, symbol: str, category: str) -> tuple[float, str, float]:
    positions = order_manager.fetch_positions(symbol, category)
    for position in positions:
        side = str(position.get("side") or position.get("positionSide") or "").lower()
        qty = float(position.get("size") or position.get("positionQty") or 0.0)
        if side in {"long", "buy"} and qty > 0:
            avg_price = (
                float(position.get("avgPrice") or position.get("entryPrice") or 0.0)
                or float(position.get("averagePrice") or position.get("avg_entry_price") or 0.0)
            )
            return qty, side, avg_price
    raise SystemExit("No active long position found to test trailing fallback.")


def build_fallback_order(
    order_manager: BybitOrderManager,
    symbol: str,
    category: str,
    qty: float,
    trigger_price: float,
    current_price: float,
) -> tuple[str | None, str | None, bool]:
    client_order_id = f"trailing-fallback-{int(time.time() * 1000)}"
    normalized_qty = order_manager.normalize_qty(symbol, qty, category)
    best_ask = order_manager.fetch_best_ask(symbol, category)
    reference_ask = max(best_ask, current_price) if best_ask else current_price
    raw_price = max(
        trigger_price,
        reference_ask * (1 + SAFE_OFFSET_PCT),
    )
    safe_price = order_manager.normalize_price(symbol, raw_price, category)
    print(
        "[price-debug] "
        f"current_price={current_price:.8f} "
        f"raw_trigger={trigger_price:.8f} "
        f"raw_price={raw_price:.8f} "
        f"final_price={safe_price:.8f}"
    )
    print(
        "[orderbook-debug] "
        f"best_ask={best_ask} "
        f"reference_ask={reference_ask:.8f} "
        f"final_price={safe_price:.8f}"
    )
    print(
        "[fallback-debug] creating visible fallback LIMIT order "
        f"symbol={symbol} category={category} side=Sell qty={qty} "
        f"position_idx=1 limit_price={safe_price:.8f} "
        "orderType=Limit timeInForce=GTC reduceOnly=True"
    )
    body = {
        "category": category,
        "symbol": symbol.upper(),
        "side": "Sell",
        "orderType": "Limit",
        "qty": f"{normalized_qty}",
        "price": f"{safe_price}",
        "positionIdx": 2,
        "reduceOnly": True,
        "timeInForce": "GTC",
        "orderLinkId": client_order_id,
    }
    response = order_manager._post("/v5/order/create", json.dumps(body))
    order_id = None
    if isinstance(response, dict):
        order_id = (response.get("result") or {}).get("orderId")
    ret_code = response.get("retCode") if isinstance(response, dict) else None
    ret_msg = response.get("retMsg") if isinstance(response, dict) else None
    order_accepted = (
        isinstance(response, dict)
        and ret_code in (0, "0")
        and bool(order_id)
    )
    print(
        "[fallback-debug] visible limit order response "
        f"client_order_id={client_order_id} "
        f"retCode={ret_code} retMsg={ret_msg} "
        f"order_id={order_id} raw_response={response}"
    )
    print(
        "[fallback-debug] acceptance "
        f"accepted={order_accepted} "
        f"retCode={ret_code} "
        f"order_id={order_id}"
    )
    if order_accepted:
        print(
            "[info] Visible reduce-only LIMIT fallback order accepted by Bybit. "
            "It should appear in Open Orders until filled or canceled."
        )
    if not isinstance(response, dict):
        print("[fallback-error] non-dict response from place_reduce_market_order")
    elif ret_code not in (0, "0"):
        print("[fallback-error] Bybit rejected fallback order request")
    elif not order_id:
        print("[fallback-error] fallback order accepted payload-wise but no orderId returned")
    if not order_accepted:
        print(
            "[fallback-debug] rejected order details "
            f"client_id={client_order_id} exchange_id={order_id}"
        )
    return client_order_id, order_id, order_accepted


def cancel_order(order_manager: BybitOrderManager, order_id: str, symbol: str, category: str) -> None:
    if not order_id:
        return
    canceled = order_manager.cancel_order(order_id, symbol=symbol, category=category)
    print(f"[fallback] cancel_requested order_id={order_id} success={canceled}")
    print(f"[cancel-debug] requested cancel for order_id={order_id}")


def fetch_open_order(
    order_manager: BybitOrderManager,
    symbol: str,
    order_id: str | None,
    client_id: str | None,
    category: str,
) -> dict | None:
    params = {
        "category": category,
        "symbol": symbol.upper(),
    }
    if order_id:
        params["orderId"] = order_id
    if client_id:
        params["orderLinkId"] = client_id
    data = order_manager._get("/v5/order/realtime", params)
    if not data:
        return None
    result = data.get("result") or {}
    orders = result.get("list") or []
    if not orders:
        return None

    for order in orders:
        order_id_match = not order_id or str(order.get("orderId")) == str(order_id)
        client_id_match = not client_id or str(order.get("orderLinkId")) == str(client_id)
        if not (order_id_match and client_id_match):
            continue
        status = (order.get("orderStatus") or order.get("status") or "").upper()
        if status not in {"NEW", "PARTIALLYFILLED", "PARTIALLY_FILLED", "UNTRIGGERED"}:
            continue
        return order

    return None


def is_order_gone_from_open_orders(
    order_manager: BybitOrderManager,
    symbol: str,
    order_id: str | None,
    client_id: str | None,
    category: str,
) -> bool:
    return fetch_open_order(order_manager, symbol, order_id, client_id, category) is None


def is_order_canceled_in_history(
    order_manager: BybitOrderManager,
    client_id: str,
    order_id: str,
) -> bool:
    history = order_manager.fetch_order_history(order_link_id=client_id)
    if not history:
        return False
    for order in history:
        if str(order.get("orderId")) == str(order_id):
            status = (order.get("orderStatus") or order.get("status") or "").upper()
            if status in {"CANCELED", "CANCELLED"}:
                return True
    return False


def is_order_filled_in_history(
    order_manager: BybitOrderManager,
    client_id: str,
    order_id: str,
) -> bool:
    history = order_manager.fetch_order_history(order_link_id=client_id)
    if not history:
        return False
    for order in history:
        if str(order.get("orderId")) == str(order_id):
            status = (order.get("orderStatus") or order.get("status") or "").upper()
            if status == "FILLED":
                return True
    return False


def main() -> None:
    if not API_KEY or not SECRET_KEY:
        raise SystemExit("Set BYBIT_API_KEY and BYBIT_SECRET_KEY before running the test.")
    config = load_config()
    symbol = os.getenv("TEST_SYMBOL") or config["symbol"]
    category = os.getenv("TEST_CATEGORY") or config.get("category", "linear")
    order_manager = BybitOrderManager(API_KEY, SECRET_KEY)
    long_qty, _side, long_avg_price = find_long_position(order_manager, symbol, category)
    reference_price = long_avg_price
    if reference_price is None:
        raise SystemExit("Unable to fetch reference price.")
    activation_price = reference_price * (1 - ACTIVATION_DROP_PCT)

    helper = TrailingFallbackManager()
    fallback_order_id: str | None = None
    fallback_client_id: str | None = None
    fallback_active = False
    last_requote_ts = 0.0
    cancel_inflight = False
    cancel_pending_order_id: str | None = None
    cancel_pending_client_id: str | None = None
    cancel_started_ts: float | None = None
    cancel_retry_count = 0
    MAX_CANCEL_RETRIES = 1

    print(f"[test] reference_price={reference_price} activation_threshold={activation_price}")

    while True:
        current_price = order_manager.fetch_last_price(symbol, category)
        if current_price is None:
            print("[test] failed to read price, retrying...")
            time.sleep(POLL_INTERVAL_SEC)
            continue

        print(
            f"[debug] current_price={current_price:.8f} "
            f"reference_price={reference_price:.8f} "
            f"activation_price={activation_price:.8f} "
            f"triggered={current_price <= activation_price} "
            f"fallback_active={fallback_active}"
        )
        print(f"[trace] last_price={current_price:.8f}")
        print(
            f"[check] comparing current_price={current_price:.8f} "
            f"<= activation_price={activation_price:.8f}"
        )

        if not fallback_active and current_price <= activation_price:
            print("[ALERT] ACTIVATION CONDITION MET -> TRIGGERING FALLBACK")
            helper.activate(
                purpose="long_sl_trailing",
                position_idx=1,
                qty=long_qty,
                trigger_price=current_price * (1 + TRAILING_OFFSET_PCT),
                reference_price=current_price,
                trailing_offset_pct=TRAILING_OFFSET_PCT,
                requote_step_pct=REQUOTE_STEP_PCT,
            )
            fallback_active = True
            trigger_price = helper.get_next_trigger_price()
            print(
                "[activation-debug] helper state "
                f"activation_price={activation_price:.8f} "
                f"current_price={current_price:.8f} "
                f"expected_fallback_trigger={current_price * (1 + TRAILING_OFFSET_PCT):.8f} "
                f"helper_next_trigger={(trigger_price or current_price):.8f}"
            )
            print(
                "[activation-debug] calling build_fallback_order "
                f"current_price={current_price:.8f} "
                f"computed_trigger_price={(trigger_price or current_price):.8f} "
                f"long_qty={long_qty}"
            )
            fallback_client_id, fallback_order_id, order_accepted = build_fallback_order(
                order_manager,
                symbol,
                category,
                long_qty,
                trigger_price or current_price,
                current_price,
            )
            if order_accepted:
                helper.mark_submit_pending()
                helper.mark_order_live(client_id=fallback_client_id, exchange_id=fallback_order_id)
                cancel_retry_count = 0
                print("[test] activation reached, first fallback order placed")
                print(
                    "[fallback-debug] helper marked order live "
                    f"client_id={fallback_client_id} exchange_id={fallback_order_id}"
                )
            else:
                print(
                    "[fallback-error] fallback order was not accepted by Bybit; "
                    "helper order state not marked live"
                )
                print(
                    "[fallback-debug] rejected order details "
                    f"client_id={fallback_client_id} exchange_id={fallback_order_id}"
                )

        if fallback_active:
            helper.update_price(current_price)
            should_requote = helper.should_requote()
            if should_requote and not cancel_inflight:
                now = time.time()
                if now - last_requote_ts < 1.0:
                    pass
                else:
                    last_requote_ts = now
                    if not fallback_order_id:
                        print(
                            "[fallback-warn] requote requested but no fallback_order_id exists; "
                            "placing fresh fallback order directly"
                        )
                        cancel_inflight = False
                        cancel_pending_order_id = None
                        cancel_pending_client_id = None
                        cancel_started_ts = None
                        trigger_price = helper.get_next_trigger_price() or current_price
                        fallback_client_id, fallback_order_id, order_accepted = build_fallback_order(
                            order_manager,
                            symbol,
                            category,
                            long_qty,
                            trigger_price,
                            current_price,
                        )
                        if order_accepted:
                            helper.mark_submit_pending()
                            helper.mark_order_live(client_id=fallback_client_id, exchange_id=fallback_order_id)
                            cancel_retry_count = 0
                            print("[fallback-debug] fresh fallback order placed without prior cancel")
                        else:
                            print("[fallback-error] fresh fallback order placement failed")
                            fallback_client_id = None
                            fallback_order_id = None
                        time.sleep(POLL_INTERVAL_SEC)
                        continue
                    helper.mark_cancel_pending()
                    cancel_order(order_manager, fallback_order_id, symbol, category)
                    cancel_inflight = True
                    cancel_pending_order_id = fallback_order_id
                    cancel_pending_client_id = fallback_client_id
                    cancel_started_ts = time.time()
                    print("[fallback-debug] cancel inflight started")

            if cancel_inflight:
                print("[fallback-debug] cancel status check")
                if (
                    cancel_started_ts is not None
                    and time.time() - cancel_started_ts >= CANCEL_TIMEOUT_SEC
                ):
                    print(
                        "[fallback-warn] cancel inflight timeout reached; "
                        "running safety check before rebuild"
                    )

                    old_order_still_exists = fetch_open_order(
                        order_manager,
                        symbol,
                        cancel_pending_order_id,
                        cancel_pending_client_id,
                        category,
                    ) is not None

                    if old_order_still_exists:
                        if cancel_retry_count < MAX_CANCEL_RETRIES:
                            print(
                                "[fallback-warn] timed out cancel still shows as live; "
                                "retrying cancel once more"
                            )
                            if cancel_pending_order_id:
                                cancel_order(order_manager, cancel_pending_order_id, symbol, category)
                            cancel_retry_count += 1
                            cancel_started_ts = time.time()
                            time.sleep(POLL_INTERVAL_SEC)
                            continue
                        print(
                            "[fallback-warn] cancel retry limit reached; "
                            "old order still appears live, skipping rebuild for safety"
                        )
                        cancel_started_ts = time.time()
                        time.sleep(POLL_INTERVAL_SEC)
                        continue

                    print(
                        "[fallback-warn] timeout reached but old order is gone; "
                        "placing fresh fallback order safely"
                    )
                    cancel_inflight = False
                    cancel_pending_order_id = None
                    cancel_pending_client_id = None
                    cancel_started_ts = None
                    fallback_order_id = None
                    fallback_client_id = None

                    trigger_price = helper.get_next_trigger_price() or current_price
                    fallback_client_id, fallback_order_id, order_accepted = build_fallback_order(
                        order_manager,
                        symbol,
                        category,
                        long_qty,
                        trigger_price,
                        current_price,
                    )
                    if order_accepted:
                        helper.mark_submit_pending()
                        helper.mark_order_live(client_id=fallback_client_id, exchange_id=fallback_order_id)
                        cancel_retry_count = 0
                        print("[fallback-debug] fresh fallback order placed after safe timeout recovery")
                    else:
                        print("[fallback-error] fallback order placement failed after safe timeout recovery")
                        fallback_client_id = None
                        fallback_order_id = None
                    time.sleep(POLL_INTERVAL_SEC)
                    continue
                cancel_done = False
                if is_order_gone_from_open_orders(
                    order_manager,
                    symbol,
                    cancel_pending_order_id,
                    cancel_pending_client_id,
                    category,
                ):
                    cancel_done = True
                elif cancel_pending_order_id and cancel_pending_client_id and is_order_canceled_in_history(
                    order_manager,
                    cancel_pending_client_id,
                    cancel_pending_order_id,
                ):
                    cancel_done = True
                if cancel_done:
                    cancel_inflight = False
                    cancel_pending_order_id = None
                    cancel_pending_client_id = None
                    cancel_started_ts = None
                    trigger_price = helper.get_next_trigger_price() or current_price
                    fallback_client_id, fallback_order_id, order_accepted = build_fallback_order(
                        order_manager,
                        symbol,
                        category,
                        long_qty,
                        trigger_price,
                        current_price,
                    )
                    if order_accepted:
                        helper.mark_submit_pending()
                        helper.mark_order_live(client_id=fallback_client_id, exchange_id=fallback_order_id)
                        cancel_retry_count = 0
                        print("[test] requote triggered, old order canceled, new fallback placed")
                        print(
                            "[fallback-debug] helper marked order live "
                            f"client_id={fallback_client_id} exchange_id={fallback_order_id}"
                        )
                        print("[fallback-debug] new fallback placed after cancel confirmation")
                    else:
                        print(
                            "[fallback-error] fallback order was not accepted by Bybit; "
                            "helper order state not marked live"
                        )
                        print(
                            "[fallback-debug] rejected order details "
                            f"client_id={fallback_client_id} exchange_id={fallback_order_id}"
                        )
                        fallback_client_id = None
                        fallback_order_id = None

            if fallback_order_id and fallback_client_id and is_order_filled_in_history(
                order_manager, fallback_client_id, fallback_order_id
            ):
                helper.mark_filled()
                print("[test] fallback order filled, helper reset")
                break

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
