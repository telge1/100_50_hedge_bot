# L1 V3AX — Entry-Forensik (V3AV, 32 Trades)

**Snapshot:** `l1_v3as_drawings_snapshot_9db7d9418813ad66.json` SHA `9db7d9418813ad66…`
**Modus:** READ-ONLY · Kein Backtest · Nur Entry-Prüfung

## Abschlussentscheidung

- **Entry-FSM / V3AK-C1:** Für alle **32** Trades **unabhängig** anhand 5m-OHLC + Trendliniengeometrie **PASS**.
- **PASS:** 32 · **REVIEW:** 0 · **FAIL:** 0 (von 32)
- **Watcher-Umbau:** ändert nur Touch/Confirm/Entry-Scheduling; TP/SL/Pool/Exit unangetastet.
- **Spec-Hinweis A2:** Code `confirm_a2` verlangt **keine** strikt bullische Kerze (nur close>line & close>prev). Zusatzprüfung `confirm_bullish_spec` bei allen A2-Executions **PASS** in diesem Run.
- **B-Reclaim:** Erlaubt Touch mit Close **unter** Linie → Start `WATCHING_BELOW` ohne vorherigen „break_close_below“; Entry nach **2× close>line** (kein Docht-Exit).
- **Freeze-Empfehlung:** **Ja** — Entry-Implementierung technisch einfrierbar; **22** Chart-Episoden (`l1_v3ax_chart_review_overlay.json`) zur visuellen Kontrolle empfohlen.

## Übersicht

| Trade | Variante | Touch | Confirm | Entry | Bars T→E | Prüfung |
|---|---|---|---|---:|---:|---|
| `te-d-e0ea2ce2fb44-0016` | V3AK | 2026-07-14 02:00:00 UTC | 2026-07-14 02:05:00 UTC | 2026-07-14 02:10:00 UTC | 2 | **PASS** — V3AK C1 + 0.20% zone |
| `te-d-e0ea2ce2fb44-0029` | V3AK | 2026-07-19 18:05:00 UTC | 2026-07-19 18:10:00 UTC | 2026-07-19 18:15:00 UTC | 2 | **PASS** — V3AK C1 + 0.20% zone |
| `te-d-e0ea2ce2fb44-0034` | V3AK | 2026-07-20 19:35:00 UTC | 2026-07-20 19:40:00 UTC | 2026-07-20 19:45:00 UTC | 2 | **PASS** — V3AK C1 + 0.20% zone |
| `te-d-755b9ca8482c-0011` | V3AK | 2026-07-25 05:45:00 UTC | 2026-07-25 05:50:00 UTC | 2026-07-25 05:55:00 UTC | 2 | **PASS** — V3AK C1 + 0.20% zone |
| `te-d-8037f795d49c-0004` | V3AK | 2026-08-10 13:30:00 UTC | 2026-08-10 13:35:00 UTC | 2026-08-10 13:40:00 UTC | 2 | **PASS** — V3AK C1 + 0.20% zone |
| `te-d-6df07d085e08-0003` | V3AK | 2026-09-02 15:10:00 UTC | 2026-09-02 15:15:00 UTC | 2026-09-02 15:20:00 UTC | 2 | **PASS** — V3AK C1 + 0.20% zone |
| `te-d-6df07d085e08-0004` | V3AK | 2026-09-02 17:20:00 UTC | 2026-09-02 17:25:00 UTC | 2026-09-02 17:30:00 UTC | 2 | **PASS** — V3AK C1 + 0.20% zone |
| `te-d-6df07d085e08-0006` | V3AK | 2026-09-02 22:05:00 UTC | 2026-09-02 22:10:00 UTC | 2026-09-02 22:15:00 UTC | 2 | **PASS** — V3AK C1 + 0.20% zone |
| `te-d-6df07d085e08-0010` | V3AK | 2026-09-03 00:30:00 UTC | 2026-09-03 00:35:00 UTC | 2026-09-03 00:40:00 UTC | 2 | **PASS** — V3AK C1 + 0.20% zone |
| `te-d-e0ea2ce2fb44-0002` | Watcher_A2 | 2026-07-08 14:45:00 UTC | 2026-07-08 14:55:00 UTC | 2026-07-08 15:00:00 UTC | 3 | **PASS** — A2 all code rules |
| `te-d-e0ea2ce2fb44-0003` | Watcher_A2 | 2026-07-08 15:05:00 UTC | 2026-07-08 15:45:00 UTC | 2026-07-08 15:50:00 UTC | 9 | **PASS** — A2 all code rules |
| `te-d-e0ea2ce2fb44-0007` | Watcher_A2 | 2026-07-14 03:15:00 UTC | 2026-07-14 03:35:00 UTC | 2026-07-14 03:40:00 UTC | 5 | **PASS** — A2 all code rules |
| `te-d-e0ea2ce2fb44-0012` | Watcher_A2 | 2026-07-19 18:05:00 UTC | 2026-07-19 18:10:00 UTC | 2026-07-19 18:15:00 UTC | 2 | **PASS** — A2 all code rules |
| `te-d-e0ea2ce2fb44-0016` | Watcher_A2 | 2026-07-20 19:35:00 UTC | 2026-07-20 19:40:00 UTC | 2026-07-20 19:45:00 UTC | 2 | **PASS** — A2 all code rules |
| `te-d-755b9ca8482c-0006` | Watcher_A2 | 2026-07-25 08:05:00 UTC | 2026-07-25 08:10:00 UTC | 2026-07-25 08:15:00 UTC | 2 | **PASS** — A2 all code rules |
| `te-d-8037f795d49c-0003` | Watcher_A2 | 2026-08-10 15:10:00 UTC | 2026-08-10 15:45:00 UTC | 2026-08-10 15:50:00 UTC | 8 | **PASS** — A2 all code rules |
| `te-d-8037f795d49c-0008` | Watcher_A2 | 2026-08-10 22:00:00 UTC | 2026-08-10 22:10:00 UTC | 2026-08-10 22:15:00 UTC | 3 | **PASS** — A2 all code rules |
| `te-d-6df07d085e08-0001` | Watcher_A2 | 2026-09-02 10:50:00 UTC | 2026-09-02 10:55:00 UTC | 2026-09-02 11:00:00 UTC | 2 | **PASS** — A2 all code rules |
| `te-d-6df07d085e08-0002` | Watcher_A2 | 2026-09-02 22:20:00 UTC | 2026-09-02 22:35:00 UTC | 2026-09-02 22:40:00 UTC | 4 | **PASS** — A2 all code rules |
| `te-d-e0ea2ce2fb44-0002` | Watcher_A3 | 2026-07-08 14:45:00 UTC | 2026-07-08 15:45:00 UTC | 2026-07-08 15:50:00 UTC | 13 | **PASS** — A3 rules |
| `te-d-e0ea2ce2fb44-0003` | Watcher_A3 | 2026-07-14 03:15:00 UTC | 2026-07-14 03:35:00 UTC | 2026-07-14 03:40:00 UTC | 5 | **PASS** — A3 rules |
| `te-d-e0ea2ce2fb44-0006` | Watcher_A3 | 2026-07-20 19:35:00 UTC | 2026-07-20 19:40:00 UTC | 2026-07-20 19:45:00 UTC | 2 | **PASS** — A3 rules |
| `te-d-755b9ca8482c-0004` | Watcher_A3 | 2026-07-24 16:45:00 UTC | 2026-07-24 17:15:00 UTC | 2026-07-24 17:20:00 UTC | 7 | **PASS** — A3 rules |
| `te-d-8037f795d49c-0002` | Watcher_A3 | 2026-08-10 16:20:00 UTC | 2026-08-10 16:35:00 UTC | 2026-08-10 16:40:00 UTC | 4 | **PASS** — A3 rules |
| `te-d-6df07d085e08-0001` | Watcher_A3 | 2026-09-02 10:50:00 UTC | 2026-09-02 10:55:00 UTC | 2026-09-02 11:00:00 UTC | 2 | **PASS** — A3 rules |
| `te-d-6df07d085e08-0002` | Watcher_A3 | 2026-09-02 22:20:00 UTC | 2026-09-02 23:05:00 UTC | 2026-09-02 23:10:00 UTC | 10 | **PASS** — A3 rules |
| `te-d-e0ea2ce2fb44-0002` | Watcher_B | 2026-07-08 14:45:00 UTC | 2026-07-08 14:55:00 UTC | 2026-07-08 15:00:00 UTC | 3 | **PASS** — B reclaim (Touch→BELOW, 2× close>line) |
| `te-d-e0ea2ce2fb44-0003` | Watcher_B | 2026-07-08 15:05:00 UTC | 2026-07-08 15:40:00 UTC | 2026-07-08 15:45:00 UTC | 8 | **PASS** — B reclaim (Touch→BELOW, 2× close>line) |
| `te-d-e0ea2ce2fb44-0008` | Watcher_B | 2026-07-14 03:15:00 UTC | 2026-07-14 03:35:00 UTC | 2026-07-14 03:40:00 UTC | 5 | **PASS** — B reclaim (Touch→BELOW, 2× close>line) |
| `te-d-8037f795d49c-0002` | Watcher_B | 2026-08-10 15:10:00 UTC | 2026-08-10 15:30:00 UTC | 2026-08-10 15:35:00 UTC | 5 | **PASS** — B reclaim (Touch→BELOW, 2× close>line) |
| `te-d-8037f795d49c-0004` | Watcher_B | 2026-08-10 22:00:00 UTC | 2026-08-10 22:10:00 UTC | 2026-08-10 22:15:00 UTC | 3 | **PASS** — B reclaim (Touch→BELOW, 2× close>line) |
| `te-d-6df07d085e08-0001` | Watcher_B | 2026-09-02 22:20:00 UTC | 2026-09-02 22:35:00 UTC | 2026-09-02 22:40:00 UTC | 4 | **PASS** — B reclaim (Touch→BELOW, 2× close>line) |

## Eindeutige Entry-Episoden (Chart): **22**

Detail: `l1_v3ax_chart_unique_events.json`, Overlay: `l1_v3ax_chart_review_overlay.json`

## Artefakte
- `l1_v3ax_entry_audit_full.json` — Regel-Checks + Transitionen (B)
- `l1_v3ax_entry_audit_table.csv`

**STOP**