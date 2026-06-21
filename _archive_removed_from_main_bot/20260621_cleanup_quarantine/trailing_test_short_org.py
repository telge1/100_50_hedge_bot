import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv


def resolve_project_root() -> Path:
    current = Path(__file__).resolve()
    for candidate in [current.parent, *current.parents]:
        if (candidate / "fixed_cycle_hedge_bot").exists() and (candidate / "env").exists():
            return candidate
    return current.parent


ROOT = resolve_project_root()
sys.path.insert(0, str(ROOT))

env_local = ROOT / "env" / ".env.local"
env_default = ROOT / "env" / ".env"

if env_local.exists():
    load_dotenv(env_local)
elif env_default.exists():
    load_dotenv(env_default)

from fixed_cycle_hedge_bot.order_manager import BybitOrderManager

CONFIG_PATH = ROOT / "fixed_cycle_hedge_bot" / "config" / "fixed_cycle_config.json"

# API-Keys exakt wie im alten Code-Stil
API_KEY = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY")
SECRET_KEY = os.getenv("BYBIT_API_SECRET") or os.getenv("SECRET_KEY")

# Ziel-Logik für SHORT + cancel/replace Stop-Order
POSITION_IDX = 2                  # Hedge Mode SHORT side
ACTIVATION_DROP_PCT = 0.001      # erster Aktivierungspreis: 0.5% unter Short-Avg
REQUOTE_STEP_PCT = 0.001          # neue Aktivierungsstufe: weitere 0.2% tiefer
STOP_OFFSET_PCT = 0.0025          # Stop-Preis liegt 0.3% über Aktivierungspreis
PARTIAL_CLOSE_PCT = 0.5           # 50% der Short-Positionsgröße
TRIGGER_BY = "LastPrice"
POLL_INTERVAL_SEC = 0.2
CANCEL_TIMEOUT_SEC = 5.0
QTY_EPSILON = 1e-12

LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "partial_short_stop_ladder.log"


def setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("partial_short_stop_ladder")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    logger.info("=" * 100)
    logger.info("START partial_short_stop_ladder")
    logger.info("=" * 100)
    logger.info(f"[paths] ROOT={ROOT}")
    logger.info(f"[paths] env_local_exists={env_local.exists()} env_default_exists={env_default.exists()}")
    logger.info(f"[paths] CONFIG_PATH={CONFIG_PATH}")

    masked_key = f"{API_KEY[:4]}***" if API_KEY else None
    masked_secret = f"{SECRET_KEY[:4]}***" if SECRET_KEY else None
    logger.info(f"[env] API_KEY_present={bool(API_KEY)} API_KEY_preview={masked_key}")
    logger.info(f"[env] SECRET_KEY_present={bool(SECRET_KEY)} SECRET_KEY_preview={masked_secret}")

    return logger


logger = setup_logger()


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as config_fp:
        return json.load(config_fp)


def get_short_position(
    order_manager: BybitOrderManager,
    symbol: str,
    category: str,
) -> tuple[float, str, float] | None:
    positions = order_manager.fetch_positions(symbol, category)
    for position in positions:
        side = str(position.get("side") or position.get("positionSide") or "").lower()
        qty = float(position.get("size") or position.get("positionQty") or 0.0)
        if side in {"short", "sell"} and qty > 0:
            avg_price = (
                float(position.get("avgPrice") or position.get("entryPrice") or 0.0)
                or float(position.get("averagePrice") or position.get("avg_entry_price") or 0.0)
            )
            return qty, side, avg_price
    return None


def create_partial_short_stop_order(
    order_manager: BybitOrderManager,
    symbol: str,
    category: str,
    qty: float,
    stop_price: float,
) -> tuple[str | None, str | None, bool]:
    client_order_id = f"short-stop-{int(time.time() * 1000)}"
    normalized_qty = order_manager.normalize_qty(symbol, qty, category)
    trigger_price = order_manager.normalize_price(symbol, stop_price, category)

    body = {
        "category": category,
        "symbol": symbol.upper(),
        "side": "Buy",
        "orderType": "Market",
        "qty": str(normalized_qty),
        "triggerPrice": str(trigger_price),
        "triggerDirection": 1,
        "triggerBy": TRIGGER_BY,
        "positionIdx": POSITION_IDX,
        "reduceOnly": True,
        "closeOnTrigger": True,
        "orderLinkId": client_order_id,
    }

    logger.info(f"[request] create partial short stop body={body}")
    response = order_manager._post("/v5/order/create", json.dumps(body))
    logger.info(f"[response] create partial short stop response={response}")

    order_id = None
    if isinstance(response, dict):
        order_id = (response.get("result") or {}).get("orderId")

    ret_code = response.get("retCode") if isinstance(response, dict) else None
    order_accepted = isinstance(response, dict) and ret_code in (0, "0") and bool(order_id)

    logger.info(
        f"[create-stop] client_order_id={client_order_id} "
        f"order_id={order_id} accepted={order_accepted} retCode={ret_code}"
    )

    return client_order_id, order_id, order_accepted


def cancel_stop_order(
    order_manager: BybitOrderManager,
    symbol: str,
    category: str,
    order_id: str | None,
    order_link_id: str | None,
) -> bool:
    if not order_id and not order_link_id:
        return False

    body = {
        "category": category,
        "symbol": symbol.upper(),
    }
    if order_id:
        body["orderId"] = order_id
    elif order_link_id:
        body["orderLinkId"] = order_link_id

    logger.info(f"[request] cancel stop order body={body}")
    response = order_manager._post("/v5/order/cancel", json.dumps(body))
    logger.info(f"[response] cancel stop order response={response}")

    if not isinstance(response, dict):
        return False
    return response.get("retCode") in (0, "0")


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


def wait_for_cancel_confirmation(
    order_manager: BybitOrderManager,
    symbol: str,
    category: str,
    order_id: str | None,
    client_id: str | None,
    timeout_sec: float,
) -> bool:
    started = time.time()
    while time.time() - started < timeout_sec:
        if is_order_gone_from_open_orders(order_manager, symbol, order_id, client_id, category):
            logger.info("[cancel-check] order gone from open orders")
            return True
        if order_id and client_id and is_order_canceled_in_history(order_manager, client_id, order_id):
            logger.info("[cancel-check] order found canceled in history")
            return True
        time.sleep(POLL_INTERVAL_SEC)
    logger.warning("[cancel-check] cancel confirmation timeout reached")
    return False


def main() -> None:
    if not API_KEY or not SECRET_KEY:
        raise SystemExit("Set BYBIT_API_KEY and BYBIT_API_SECRET before running the test.")

    config = load_config()
    symbol = config["symbol"]
    category = config.get("category", "linear")

    logger.info(f"[config] symbol={symbol} category={category}")
    logger.info(
        "[config] "
        f"POSITION_IDX={POSITION_IDX} "
        f"ACTIVATION_DROP_PCT={ACTIVATION_DROP_PCT} "
        f"REQUOTE_STEP_PCT={REQUOTE_STEP_PCT} "
        f"STOP_OFFSET_PCT={STOP_OFFSET_PCT} "
        f"PARTIAL_CLOSE_PCT={PARTIAL_CLOSE_PCT} "
        f"TRIGGER_BY={TRIGGER_BY}"
    )

    order_manager = BybitOrderManager(API_KEY, SECRET_KEY)

    initial_position = get_short_position(order_manager, symbol, category)
    if initial_position is None:
        raise SystemExit("No active short position found to test stop-order ladder.")

    initial_short_qty, _side, short_avg_price = initial_position
    reference_price = short_avg_price

    if reference_price <= 0:
        raise SystemExit("Invalid short average price.")
    if not (0 < PARTIAL_CLOSE_PCT <= 1):
        raise SystemExit("PARTIAL_CLOSE_PCT must be > 0 and <= 1.")
    if ACTIVATION_DROP_PCT <= 0:
        raise SystemExit("ACTIVATION_DROP_PCT must be > 0.")
    if REQUOTE_STEP_PCT <= 0:
        raise SystemExit("REQUOTE_STEP_PCT must be > 0.")
    if STOP_OFFSET_PCT <= 0:
        raise SystemExit("STOP_OFFSET_PCT must be > 0.")

    partial_qty_raw = initial_short_qty * PARTIAL_CLOSE_PCT
    partial_qty = order_manager.normalize_qty(symbol, partial_qty_raw, category)
    if partial_qty <= 0:
        raise SystemExit("Computed partial stop qty is zero after normalization.")

    current_activation_raw = reference_price * (1 - ACTIVATION_DROP_PCT)
    current_activation_price = order_manager.normalize_price(symbol, current_activation_raw, category)

    current_stop_raw = current_activation_price * (1 + STOP_OFFSET_PCT)
    current_stop_price = order_manager.normalize_price(symbol, current_stop_raw, category)

    active_order_id: str | None = None
    active_client_id: str | None = None
    levels_sent = 0

    logger.info(
        f"[init] found short position symbol={symbol} qty={initial_short_qty} avg_price={short_avg_price}"
    )
    logger.info(f"[init] partial_qty={partial_qty}")
    logger.info(f"[init] initial_activation_price={current_activation_price}")
    logger.info(f"[init] initial_stop_price={current_stop_price}")
    logger.info(f"[init] polling every {POLL_INTERVAL_SEC} seconds")
    logger.info(f"[init] log_file={LOG_FILE}")

    try:
        while True:
            live_position = get_short_position(order_manager, symbol, category)
            if live_position is None:
                logger.info("[state] no active short position found anymore -> stopping watcher")
                break

            current_short_qty, _live_side, _live_avg = live_position

            if current_short_qty + QTY_EPSILON < initial_short_qty:
                logger.info(
                    "[state] short position size reduced -> assume stop order triggered "
                    f"(initial_qty={initial_short_qty}, current_qty={current_short_qty})"
                )
                break

            current_price = order_manager.fetch_last_price(symbol, category)
            if current_price is None:
                logger.warning("[watch] failed to fetch current price, retrying...")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            logger.info(
                f"[watch] current_price={current_price:.8f} "
                f"activation_price={current_activation_price:.8f} "
                f"stop_price={current_stop_price:.8f} "
                f"levels_sent={levels_sent} "
                f"active_order_id={active_order_id} "
                f"active_client_id={active_client_id}"
            )

            if current_price > current_activation_price:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            logger.info("[alert] activation level reached -> cancel old stop (if any) and place new stop order")

            if active_order_id or active_client_id:
                cancel_ok = cancel_stop_order(
                    order_manager=order_manager,
                    symbol=symbol,
                    category=category,
                    order_id=active_order_id,
                    order_link_id=active_client_id,
                )
                if not cancel_ok:
                    raise SystemExit("Failed to send cancel for previous stop order.")

                confirmed = wait_for_cancel_confirmation(
                    order_manager=order_manager,
                    symbol=symbol,
                    category=category,
                    order_id=active_order_id,
                    client_id=active_client_id,
                    timeout_sec=CANCEL_TIMEOUT_SEC,
                )
                if not confirmed:
                    raise SystemExit("Previous stop order cancel was not confirmed in time.")

                logger.info(
                    f"[cancelled] old stop removed order_id={active_order_id} client_id={active_client_id}"
                )
                active_order_id = None
                active_client_id = None

            client_id, order_id, accepted = create_partial_short_stop_order(
                order_manager=order_manager,
                symbol=symbol,
                category=category,
                qty=partial_qty,
                stop_price=current_stop_price,
            )
            if not accepted or not order_id:
                raise SystemExit("New partial short stop order was not accepted.")

            active_client_id = client_id
            active_order_id = order_id
            levels_sent += 1

            next_activation_raw = current_activation_price * (1 - REQUOTE_STEP_PCT)
            next_activation_price = order_manager.normalize_price(symbol, next_activation_raw, category)

            next_stop_raw = next_activation_price * (1 + STOP_OFFSET_PCT)
            next_stop_price = order_manager.normalize_price(symbol, next_stop_raw, category)

            logger.info(
                f"[armed] new stop order live order_id={active_order_id} "
                f"client_id={active_client_id} "
                f"next_activation_price={next_activation_price:.8f} "
                f"next_stop_price={next_stop_price:.8f}"
            )

            current_activation_price = next_activation_price
            current_stop_price = next_stop_price

            if active_order_id and active_client_id and is_order_filled_in_history(
                order_manager, active_client_id, active_order_id
            ):
                logger.info("[history] stop order already filled according to history")
                break

            time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        logger.info("[shutdown] stopped by user before stop ladder completed")
        return
    except Exception:
        logger.exception("[fatal] unexpected exception in watcher")
        raise
    finally:
        logger.info("END partial_short_stop_ladder")
        logger.info("=" * 100)


if __name__ == "__main__":
    main()