# PHASE BT-D18 — Causal Short Sell-Limit Fill Engine

**Datum:** 2026-10-25  
**Projekt:** `pools+ob+delta_bot`  
**Verdict:** `CAUSAL_SELL_LIMIT_EXECUTION_PASS`

---

## Problem (BT-D17)

24h-Pilot: 3 Signale, **0 Trades** bei allen Entry-Modi.  
`legacy_v1`: Fill nur bei `low <= limit <= high` — **marketable** Sell-Limits (`open >= limit`, `low > limit`) wurden nicht ausgeführt.  
Zusätzlich: **Pool-Cancel vor Fill-Prüfung** auf derselben Bar.

## Lösung

| Artefakt | Rolle |
|----------|--------|
| `sell_limit_fill_v1.py` | `legacy_v1` + `causal_sell_limit_v2` |
| `short_research_engine_v1.py` | Fill **vor** Pool-Cancel; Ledger `FILLED` + `fill_rule_id` + `ohlc_evidence` |
| Config / YAML | `short_entry.pending_limit_fill_model` (Default JSON/YAML fehlt → `legacy_v1`) |
| `configs/backtester.yaml` | `causal_sell_limit_v2` (explizit, kein stillschweigendes Überschreiben alter JSON-Runs) |

### `causal_sell_limit_v2` (SHORT sell limit L)

| Bedingung (ab Aktivierung) | Aktion |
|----------------------------|--------|
| `high < L` | kein Touch |
| `open >= L` | Fill am **Open** (konservativ für Short: höherer Entry) |
| `open < L` und `high >= L` | Fill bei **L** |
| Bar vor `order_active_from_utc` | kein Fill |

**Priorität:** Auf aktiver Bar zuerst Fill-Evaluation, danach `CANCELLED_POOL_INACTIVE` (Touch vor Close-Density-Verlust).

**Unverändert:** SL an Pool-Oberkante, TP `select_short_tp_depth2(fill, …)`, BT-D6-PnL, `intrabar_ambiguity_policy`.

## Baseline-Sicherung

- Archiv: `results/phase_bt_d18_causal_sell_limit_execution_v1/baseline_archive/`
- Semantik-Dokument: `LEGACY_EXECUTION_SEMANTICS_V1.md`
- `latest_result.json` bei Vergleichsläufen **nicht** aktualisiert (`--no-publish-latest` / Comparison-Skript)

## Tests

```bash
pytest tools/phase_bt_d7_config_driven_short_pnl_integration_v1/tests/test_sell_limit_fill_v1.py \
       tools/phase_bt_d7_config_driven_short_pnl_integration_v1/tests/test_entry_price_v1.py \
       tools/phase_bt_d7_config_driven_short_pnl_integration_v1/tests/test_bt_d7_integration_v1.py \
       tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/tests/test_config_yaml_v1.py
```

**25+ passed** — u. a. marketable Open, intrabar Touch, legacy-Regression, Fill vor Pool-Cancel, Fees/PnL-Pfad über Engine-Integration.

## Historischer 24h-Vergleich (BT-D9-Cache)

Skript: `tools/phase_bt_d18_causal_sell_limit_execution_v1/run_historical_comparison.py`  
Index: `results/phase_bt_d18_causal_sell_limit_execution_v1/comparison_index.json`

| Variante | legacy_v1 Fills/Trades | causal_v2 Fills/Trades | Netto-PnL |
|----------|------------------------|-------------------------|-----------|
| A lower_edge | 0 / 0 | 0 / 0 | 0 |
| B upper_edge | 0 / 0 | 0 / 0 | 0 |
| C offset 0,1 % | 0 / 0 | 0 / 0 | 0 |

**Interpretation:** Engine-Fix ist synthetisch verifiziert; im 24h-Fenster 11.09.2026 bleiben alle Varianten ohne abgeschlossenen Trade (u. a. kein `open>=limit` / kein Bracket-Touch vor Cancel — konsistent mit BT-D13-Diagnose). Die Signale sind **diagnostische Beispiele**, kein Strategietest.

## Bekannte Grenzen

- Keine intrabar-Reihenfolge Open→High→Low innerhalb der 5m-Kerze; SL/TP-Konflikt auf Entry-Bar weiter über `intrabar_ambiguity_policy`.
- Density nur am **Close** — Pool-„Touch-Deaktivierung“ innerhalb der Bar nicht modelliert außer Fill-vor-Cancel-Regel.

## STOP

Kein Dashboard, kein SSOT/Density-Change, keine Mehrmonats-Runs.
