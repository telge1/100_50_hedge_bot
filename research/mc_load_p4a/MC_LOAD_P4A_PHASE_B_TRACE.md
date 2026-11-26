# P4A — Phase-B Request Trace

## Frontend (`history-phase-b`)

```javascript
loadPane(paneId, {
  gen, replayGen,
  sourceAction: "history-phase-b",
  preserveView: true,
  skipFastFirst: true,
  _historyPhaseB: true,
  allowStale: true,
});
```

`reqBody` (kein Phase-A-Fenster):

```json
{
  "symbol": "SOLUSDT",
  "timeframe": "5m",
  "limit": 24192,
  "ema": { ... },
  "stochastic": { ... },
  "open_interest": { ... },
  "liquidity": { ... },
  "allow_stale": true
}
```

`limit` = `weeks * (7*24*3600/300)` = `12 * 2016` = **24192**.

## Backend

`api.py` → `pane_bundle(..., start=None, end=None, limit=24192)`.

`pane_bundle` → `resolve_candle_pack` (kein sichtbares `from`/`to`).

## Cache

Key-Tupel: `(sym, timeframe, start, end, lim)`.

TTL: `_HISTORY_TTL` (45s) wenn `end` nahe „jetzt“, sonst variabel.

Hit-Antwort **vor P4A**:

```python
"candles": candles_to_payload(cached)  # volle Liste
"limit": lim                            # 24192
"cache": "hit"
```

Hit-Antwort **nach P4A**:

```python
trimmed = _tail_limit_candles(cached, None, None, lim)
"candles": candles_to_payload(trimmed)  # max lim
```

## GO-TO / Replay

Wenn `start`/`end` im Body: **kein** Tail-Trim (Fensterladen unverändert).

Warmup in `pane_bundle` (`load_start < visible_start`) nutzt denselben `resolve_candle_pack`-Key inkl. `start=load_start` — getrennt von unbounded Phase B.

## Erwartung nach Fix

Phase-B-HTTP: `candle_count === request_limit === 24192` (±0–1 forming), `cache: hit` erlaubt, Länge trotzdem korrekt.