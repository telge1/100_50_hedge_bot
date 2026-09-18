"""HTTP localhost client for public-trade fanout IPC (127.0.0.1 only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class HttpTradeFanoutClient:
    """Talks to CollectorControlService trade_fanout routes on localhost."""

    base_url: str = "http://127.0.0.1:8787"
    timeout_sec: float = 5.0

    def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8") if exc.fp else ""
            try:
                return json.loads(raw) if raw else {"ok": False, "error": str(exc)}
            except json.JSONDecodeError:
                return {"ok": False, "error": str(exc), "body": raw[:300]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def create_trade_subscriber(self, **fields: Any) -> dict[str, Any]:
        return self._post("/api/trade_fanout/create_trade_subscriber", fields)

    def poll_trade_events(self, **fields: Any) -> dict[str, Any]:
        return self._post("/api/trade_fanout/poll_trade_events", fields)

    def trade_subscriber_heartbeat(self, **fields: Any) -> dict[str, Any]:
        return self._post("/api/trade_fanout/trade_subscriber_heartbeat", fields)

    def remove_trade_subscriber(self, **fields: Any) -> dict[str, Any]:
        return self._post("/api/trade_fanout/remove_trade_subscriber", fields)

    def trade_fanout_status(self) -> dict[str, Any]:
        return self._post("/api/trade_fanout/trade_fanout_status", {})

    def trade_fanout_cleanup(self) -> dict[str, Any]:
        return self._post("/api/trade_fanout/trade_fanout_cleanup", {})


@dataclass
class InProcessTradeFanoutBridge:
    """Test/smoke bridge wrapping PublicTradeEventFanout in-process."""

    fanout: Any

    def create_trade_subscriber(self, **fields: Any) -> dict[str, Any]:
        return self.fanout.create_subscriber(**fields)

    def poll_trade_events(self, **fields: Any) -> dict[str, Any]:
        return self.fanout.poll_events(
            subscriber_id=str(fields.get("subscriber_id") or ""),
            cursor=fields.get("cursor"),
            limit=int(fields.get("limit") or 256),
        )

    def trade_subscriber_heartbeat(self, **fields: Any) -> dict[str, Any]:
        return self.fanout.heartbeat(str(fields.get("subscriber_id") or ""))

    def remove_trade_subscriber(self, **fields: Any) -> dict[str, Any]:
        return self.fanout.remove_subscriber(str(fields.get("subscriber_id") or ""))

    def trade_fanout_status(self) -> dict[str, Any]:
        return self.fanout.status()

    def trade_fanout_cleanup(self) -> dict[str, Any]:
        return self.fanout.timeout_cleanup()
