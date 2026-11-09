# Frozen pool kernel spec v1 (Phase 0–1)

## Reference pipeline (unchanged in production)

`bot/e1r_live_scanner` → `load_pane_candles` → `ui_lld_config("15m")` →
`scanner_pools_for_index` → **`available`** → strategy (`entry_setup`).

## Pool compute window vs display window

**POOL COMPUTE WINDOW** (causal, deterministic):

- Load 15m candles from **`PANE_FROM_UTC` (2026-02-10 UTC)** through at least the
  **decision bar close** for the requested `decision_timestamp`.
- With `from_unix` + `to_unix` set, `history_weeks` does not trim the pane (see
  `load_pane_candles` docstring).

**DISPLAY WINDOW** (chart UI, later phases):

- Zoom, scroll, Go-To, rolling 30d, or visible day range **must not** define this window.
- Display only chooses which subset of candles/overlays to **draw**.

```
DISPLAY WINDOW != POOL COMPUTE WINDOW
```

## Frozen config split

| Class | Source today | `frozen_lld_config_v1.py` |
|-------|----------------|---------------------------|
| Pool engine | `indicator_settings.json` → workspace | `LLD_ENGINE_V1` |
| Selection policy | `service.py` constants + `select_lld_pools_for_chart` | `LLD_SELECTION_POLICY_V1` (documented) |
| Display / clusters / colors | `indicator_settings.json` | **not frozen** (UI) |

## Signal-relevant pools

- **`available`**: used by Short `entry_setup` / Long geometry input.
- **`_ui_selected`**: chart selection before time filter — not used for signals.

## Frozen pool kernel v1 (Phase 2)

Public API: `frozen_pool_kernel_v1.available_at_decision_v1(symbol, decision_timestamp, ...)`.

- **`decision_timestamp`**: **15m bar close** (`bar_close`), same as Short `bar["close_time"]` and oracle golden JSON.
- **Not** candle open — passing bar open raises `FrozenPoolKernelError`.
- **Compute window**: load candles `from=PANE_FROM_UTC`, `to=decision_timestamp` (unix), `history_weeks=12`.
- **Config**: `frozen_lld_config_v1` only (no workspace / `indicator_settings.json`).

## Short live shadow (Phase 3A)

- `available_from_loaded_pane_v1(symbol, candles, decision_close, …)` — no ClickHouse reload; reuses scanner `candles15`.
- `bot/e1r_live_scanner`: `ScannerConfig.frozen_pool_shadow_parity` default **false**; logs to `logs/frozen_pool_parity.jsonl`.
- Production signals still use the existing `ui_lld_config` + `scanner_pools_for_index` path only.
