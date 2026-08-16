# Live Collector ↔ Dashboard Control Contract (Preflight)

**Date:** 2026-08-10  
**Status:** Preflight only — no dashboard UI and no HTTP control API exist in this repo yet.

## Finding: no dashboard frontend

Searched under `signal_generator_stoch_waves` for dashboard / frontend / API routes /
React-Vue-HTML UI / fixtures / SSE. **None found.**

What *does* exist and must drive the future dashboard contract:

| Layer | Path | Relevance |
| ----- | ---- | --------- |
| Health snapshot | `src/signal_generator/bybit/live/health.py` | Runtime collector status (`HealthState.to_dict`) |
| Collector FSM | `src/signal_generator/bybit/live/collector.py` | STARTING→RECOVERING→LIVE; SIGTERM stop |
| WS | `src/signal_generator/bybit/live/ws_kline.py` | `wss://stream.bybit.com/v5/public/linear`, `kline.1.{SYMBOL}`, confirm=true, ping 20s |
| Recovery SoT | `src/signal_generator/bybit/live/recovery.py` | `MAX(open_time)` → REST fill |
| Signals (chart SoT) | `migrations/001_initial_schema.sql`, `db/signals.py` | Chart query shape |
| Process supervisor | `deploy/systemd/bybit-live-1m-collector.service` | `Restart=always` (cannot distinguish intentional STOP alone) |
| Live universe | `config/live_universe.json` | Editable symbol list (no BTC) |

There are **no** Status Cards, Chart components, Start/Stop UI controls, polling/SSE
clients, or mock JSON fixtures for a dashboard in this repository.

---

## Live universe

Path: `config/live_universe.json`

Collector / CLI must load symbols from this file (or an explicit override), never
hardcode the coin list. Extend by editing `symbols[]`.

---

## Recommended collector status JSON

Align field names with existing `HealthState` where possible; extend for
dashboard + supervisor needs (`desired_state`, per-symbol rows).

```json
{
  "desired_state": "RUNNING",
  "collector_state": "LIVE",
  "websocket_connected": true,
  "configured_symbols": ["APTUSDT", "DOGEUSDT"],
  "subscribed_symbols": ["APTUSDT", "DOGEUSDT"],
  "live_symbols": ["APTUSDT", "DOGEUSDT"],
  "stale_symbols": [],
  "recovering_symbols": [],
  "last_message_at": "2026-08-10T12:00:01.000+00:00",
  "last_ping_at": "2026-08-10T12:00:00.000+00:00",
  "last_pong_at": "2026-08-10T12:00:00.050+00:00",
  "reconnect_count": 0,
  "started_at": "2026-08-10T11:55:00.000+00:00",
  "updated_at": "2026-08-10T12:00:01.000+00:00",
  "last_error": null,
  "symbols": [
    {
      "symbol": "APTUSDT",
      "configured": true,
      "subscribed": true,
      "state": "LIVE",
      "last_websocket_update_at": "2026-08-10T12:00:01.000+00:00",
      "last_closed_candle_at": "2026-08-10T11:59:00.000+00:00",
      "last_persisted_open_time": "2026-08-10T11:59:00.000+00:00",
      "candle_lag_seconds": 61,
      "recovery_state": "IDLE",
      "signal_processor_state": "CAUGHT_UP",
      "signal_processing_lag_seconds": 0,
      "last_error": null
    }
  ]
}
```

### Enums

**`desired_state`:** `RUNNING` | `STOPPED`

**`collector_state` (process):** reuse / extend `CollectorState` in `health.py`:
`STARTING` | `RECOVERING` | `LIVE` | `RECONNECTING` | `DEGRADED` | `ERROR` |
`STOPPING` | `STOPPED`

**Per-symbol `state`:** `STARTING` | `RECOVERING` | `SUBSCRIBING` | `LIVE` |
`STALE` | `ERROR` | `STOPPED`

Note: `SUBSCRIBING` / per-coin states are **not** fully modeled yet in
`HealthState` (today mostly global FSM + `symbols_stale` set). Dashboard contract
requires the richer per-symbol view above; implement when wiring status export.

### Mapping from existing `HealthState.to_dict`

| Existing | Contract |
| -------- | -------- |
| `state` | `collector_state` |
| `websocket_connected` | same |
| `last_message_at` / `last_pong_at` | same (`last_ping_at` to add) |
| `reconnect_count` | same |
| `symbols_stale` | `stale_symbols` |
| `last_closed_candle_by_symbol` | feeds `last_closed_candle_at` / `last_persisted_open_time` |
| `last_error` | same (global) |

---

## Start / Stop control (backend, not shell-from-frontend)

Dashboard must **not** exec shell. Recommended thin control plane (to implement later):

| Method | Path | Body / effect |
| ------ | ---- | ------------- |
| `GET` | `/api/collector/status` | Status JSON above (poll ~1–2s or SSE later) |
| `POST` | `/api/collector/desired_state` | `{"desired_state":"RUNNING"\|"STOPPED"}` |
| `GET` | `/api/collector/desired_state` | Current desired + observed |

Persistence of `desired_state`: small local file e.g.
`results/live_collector/desired_state.json` (or systemd drop-in + control file).

### START semantics

1. Dashboard sets `desired_state=RUNNING`
2. Supervisor ensures process is up
3. Process: `STARTING → RECOVERING → SUBSCRIBING → LIVE`

### STOP semantics (graceful)

1. Dashboard sets `desired_state=STOPPED`
2. Control plane signals process (SIGTERM / internal `request_stop`)
3. Process: no new jobs → flush inserts → finish signal catch-up → close WS →
   `collector_state=STOPPED`
4. Supervisor **must not** restart while `desired_state=STOPPED`

---

## Supervisor: intentional STOP vs CRASH

Today’s unit (`Restart=always`) cannot express intentional stop.

Recommended pattern:

```text
desired_state == STOPPED  →  supervisor: do not start / do not restart
desired_state == RUNNING
  and process dead          →  supervisor: restart
                             →  Startup Recovery → Signal Catch-up → Subscribe → LIVE
```

Implementation options (later):

1. **Control file + wrapper:** systemd `Restart=on-failure` + ExecStartPre that
   exits 0-skip when desired=STOPPED; or a small supervisor daemon that reads
   desired_state and manages the collector child.
2. **systemd + two units:** keep collector `Restart=on-failure`; a
   `collector-desired.service` / path unit writes Environment and `systemctl
   stop/start` only via the control API (never from browser).

Dashboard talks only to the HTTP control API; the API owns `systemctl` / process
lifecycle.

---

## Signal chart contract

No chart UI exists. **Source of truth** is `signals` + `SignalRepository.get_signals`
(FINAL, ordered by `candle_open_time`, `signal_id`).

Recommended marker payload (1:1 with table columns; booleans as bool in JSON):

```json
{
  "signal_id": "…",
  "symbol": "APTUSDT",
  "timeframe": "15m",
  "direction": "LONG",
  "signal_type": "wave_fade",
  "signal_price": "6.12345678",
  "candle_open_time": "2026-08-10T11:45:00.000+00:00",
  "candle_close_time": "2026-08-10T12:00:00.000+00:00",
  "generated_at": "2026-08-10T12:00:00.100+00:00",
  "tier_a": true,
  "stoch_k": 12.3,
  "stoch_d": 18.1,
  "wave_state": "…",
  "selected": true,
  "trend_15m": "UP",
  "trend_30m": "UP",
  "trend_1h": "DOWN",
  "trend_4h": "UP",
  "signal_bias": "LONG",
  "strategy_version": "…",
  "generator_version": "…",
  "marker": "▲"
}
```

Marker rule: `direction=LONG → ▲`, `direction=SHORT → ▼`.  
API sketch: `GET /api/signals?symbol=&timeframe=&start=&end=` → list of rows above.

---

## Live start / resume (ClickHouse SoT)

No static live-start timestamp in config.

Per symbol on (re)start:

1. Read `MAX(open_time)` closed 1m from ClickHouse
2. Compute last fully closed wall-clock 1m
3. REST-repair missing `[last+1m, last_closed_1m]` via existing history client
4. Signal processor catch-up from `signal_processing_state` watermarks
5. Subscribe WS; mark symbol `LIVE`

New coin with no CH history: treat as cold start (full recover from listing /
configured lookback policy) — distinct from gap repair.

---

## Bybit capacity notes

| Scale | Assessment |
| ----- | ---------- |
| 10 coins | Comfortable on one public linear connection (`kline.1.*` × 10) |
| ~100 coins | Still one connection class; watch subscribe args size (public args payload max **21 000** characters). Shard subscriptions across N connections if needed; recovery REST remains the bottleneck, not WS args at 100. |

Heartbeat: client ping ~every **20s**; treat pong / any message as liveness
(already in `ws_kline.py`).
