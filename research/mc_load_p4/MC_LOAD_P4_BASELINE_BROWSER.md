# P4 — Baseline & Browser

## Reproduzierbare Baseline (vor P4-Verifikation)

**Quelle:** Nutzer-P3B-Trace, SOLUSDT, 5m, `tf-change`, Fast-First PASS.

| Metrik | Wert | Hinweis |
|--------|------|---------|
| Phase A Kerzen | 302 | |
| Phase A Dauer | 255 ms | Server-seitig / Request-Dauer |
| `first_candles_owner_release` | ~284 ms | rel. Phase-A-Start |
| `first_candles_rendered` (Proxy) | ~307 ms | Doppel-rAF-Proxy |
| Phase B Kerzen | 66.804 | siehe Limits-Doc (~33 Wochen) |
| Phase B Dauer | 9.048 ms | |
| `ema-post-catch-up` Kerzen | 50.000 | **P4-Ziel: entfällt nach B** |
| `ema-post-catch-up` Dauer | 14.152 ms | |
| `all_optional_layers_scheduled` | 36.910 s | Trace-Start; Exact-Origin-Await verdächtig |

## Parität (nicht regressieren)

- Erste sichtbare Kerzen: weiter ~300-Bereich bei tf-change.
- Historisches Scrollen / Zeitachse / Single vs Multi: manuell prüfen.
- EMA-Kurven: gleiche Server-Bundle-Quelle; kein neues Frontend-EMA.

## P4-Browser-Protokoll

1. `localStorage.setItem("mc.load.diag", "1")`
2. Hard-Reload mit `research-mc-load-p4-v1` (Template-Query)
3. SOLUSDT, 5m, TF-Wechsel oder Symbol-Wechsel
4. `__MC_LOAD_DIAG__.exportJSON()`

**Erwartung nach P4:**

- Kein Pane-POST `ema-post-catch-up` direkt nach Phase B (`ema_catch_up_decision.skipped === true`).
- `all_optional_layers_scheduled` früher, wenn Exact Origin an + Fast-First.
- Phase-B-Request: `request_limit` ≈ `weeks * 2016`.

## Status

**`BROWSER_PENDING`** — keine neuen Messwerte in dieser Session erfasst.