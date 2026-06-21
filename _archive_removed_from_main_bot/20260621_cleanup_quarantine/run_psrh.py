import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from core.websocket_client import BybitWebSocketClient
from dotenv import load_dotenv
from strategy.config import FixedCycleConfig
from strategy.psrh_strategy import PSRHStrategy

logger = logging.getLogger("psrh-runner")


def configure_logging(log_file: str) -> None:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)


def load_strategy() -> PSRHStrategy:
    env_path = Path(__file__).resolve().parents[0] / "env" / ".env.local"
    if env_path.exists():
        load_dotenv(env_path)
    config = FixedCycleConfig()
    api_key = os.getenv("BYBIT_API_KEY")
    secret_key = os.getenv("BYBIT_API_SECRET")
    if not api_key or not secret_key:
        raise RuntimeError("Missing BYBIT_API_KEY or BYBIT_API_SECRET")

    if not config.symbol:
        raise RuntimeError("Config error: symbol is missing")
    config.api_key = api_key
    config.secret_key = secret_key
    config.default_symbol = config.symbol
    config.log_file = os.getenv("PSRH_LOG_FILE", "run_psrh.log")
    configure_logging(config.log_file)
    return PSRHStrategy(config)


def start_websocket(
    strategy: PSRHStrategy,
) -> tuple[Optional[threading.Thread], Optional[BybitWebSocketClient]]:
    config = strategy.config
    if not config.api_key or not config.secret_key:
        logger.error("Missing API keys, websocket will not start")
        return None, None
    client = BybitWebSocketClient(config.api_key, config.secret_key)
    client.set_fill_callback(strategy.on_websocket_fill)
    thread = threading.Thread(target=client.run, daemon=True)
    thread.start()
    logger.info("WebSocket client started")
    return thread, client


def start_price_poller(
    strategy: PSRHStrategy,
    stop_event: threading.Event,
    interval: float = 1.0,
) -> Optional[threading.Thread]:
    manager = strategy.order_manager
    if not manager:
        logger.error("Order manager missing for price polling")
        return None

    def poll() -> None:
        logger.info("Price poller started (%0.2fs)", interval)
        try:
            while True:
                if stop_event.wait(interval):
                    break
                if not strategy._has_recovered:
                    continue
                price = manager.fetch_mark_price(
                    strategy.config.default_symbol, strategy.config.category
                )
                if price is not None:
                    strategy.on_price_update(price)
        except KeyboardInterrupt:
            logger.info("Price poller interrupted")
        except Exception as exc:
            logger.exception("Price poller error", exc_info=exc)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    return thread


def main() -> None:
    strategy = load_strategy()
    logger.info("Strategy initialized, launching WebSocket feed")
    ws_thread, ws_client = start_websocket(strategy)
    price_stop_event = threading.Event()
    price_thread = start_price_poller(strategy, price_stop_event)
    try:
        while ws_thread and ws_thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down due to interrupt")
        price_stop_event.set()
        if ws_client:
            ws_client.stop()
        strategy.stop()
        if price_thread:
            price_thread.join(timeout=2)
        if ws_thread:
            ws_thread.join(timeout=2)


if __name__ == "__main__":
    main()
