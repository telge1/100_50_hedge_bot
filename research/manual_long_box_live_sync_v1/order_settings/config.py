"""Live Bybit settings — loaded from order_settings/.env only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"
ENV_LOCAL_PATH = Path(__file__).resolve().parent / "env" / ".env.local"


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, val = s.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def load_dotenv_chain() -> None:
    """Prefer env/.env.local (user credentials), then optional order_settings/.env."""
    _load_dotenv(ENV_LOCAL_PATH)
    _load_dotenv(ENV_PATH)


@dataclass(frozen=True)
class LiveOrderSettings:
    api_key: str
    api_secret: str
    environment: str
    category: str
    symbol: str
    position_idx: int
    live_armed: bool
    order_qty: float | None
    time_in_force: str
    attach_tpsl_on_create: bool
    tpsl_mode: str
    tp_order_type: str
    sl_order_type: str
    sl_trigger_by: str
    tp_trigger_by: str
    use_activation_notional: bool
    fixed_test_notional_usd: float | None

    @property
    def writes_allowed(self) -> bool:
        return (
            self.live_armed
            and bool(self.api_key)
            and bool(self.api_secret)
            and (
                self.fixed_test_notional_usd is not None
                or (self.order_qty is not None and self.order_qty > 0)
                or self.use_activation_notional
            )
        )


def load_settings(*, env_file: Path | None = None) -> LiveOrderSettings:
    if env_file is not None:
        _load_dotenv(env_file)
    else:
        load_dotenv_chain()
    armed = os.environ.get("MANUAL_LONG_BOX_LIVE_ARMED", "0").strip() == "1"
    qty_raw = os.environ.get("MANUAL_LONG_BOX_ORDER_QTY", "").strip()
    order_qty = float(qty_raw) if qty_raw else None
    return LiveOrderSettings(
        api_key=os.environ.get("BYBIT_API_KEY", "").strip(),
        api_secret=os.environ.get("BYBIT_API_SECRET", "").strip(),
        environment=os.environ.get("BYBIT_ENVIRONMENT", "mainnet").strip(),
        category=os.environ.get("BYBIT_CATEGORY", "linear").strip(),
        symbol=os.environ.get("BYBIT_SYMBOL", "DOGEUSDT").strip().upper(),
        position_idx=int(os.environ.get("BYBIT_POSITION_IDX", "1")),
        live_armed=armed,
        order_qty=order_qty,
        time_in_force=os.environ.get("BYBIT_LIMIT_TIME_IN_FORCE", "GTC").strip(),
        attach_tpsl_on_create=os.environ.get("BYBIT_ATTACH_TPSL_ON_CREATE", "1").strip() == "1",
        tpsl_mode=os.environ.get("BYBIT_TPSL_MODE", "Full").strip(),
        tp_order_type=os.environ.get("BYBIT_TP_ORDER_TYPE", "Market").strip(),
        sl_order_type=os.environ.get("BYBIT_SL_ORDER_TYPE", "Market").strip(),
        sl_trigger_by=os.environ.get("BYBIT_SL_TRIGGER_BY", "LastPrice").strip(),
        tp_trigger_by=os.environ.get("BYBIT_TP_TRIGGER_BY", "LastPrice").strip(),
        use_activation_notional=os.environ.get(
            "MANUAL_LONG_BOX_USE_ACTIVATION_NOTIONAL", "1"
        ).strip()
        == "1",
        fixed_test_notional_usd=(
            float(os.environ.get("MANUAL_LONG_BOX_FIXED_NOTIONAL_USDT", "10"))
            if os.environ.get("MANUAL_LONG_BOX_FIXED_NOTIONAL_USDT", "10").strip()
            else None
        ),
    )


def assert_live_ready(settings: LiveOrderSettings) -> None:
    if not settings.live_armed:
        raise RuntimeError("MANUAL_LONG_BOX_LIVE_ARMED is not set to 1")
    if not settings.api_key or not settings.api_secret:
        raise RuntimeError("BYBIT_API_KEY/BYBIT_API_SECRET missing — create order_settings/.env")
    if settings.fixed_test_notional_usd is None and not settings.use_activation_notional and (
        settings.order_qty is None or settings.order_qty <= 0
    ):
        raise RuntimeError(
            "MANUAL_LONG_BOX_ORDER_QTY must be set (or enable fixed notional / activation notional)"
        )
