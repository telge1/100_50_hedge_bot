# Latest BTCUSDT Full-OB Signal Explanation v1

**Verdict:** `BTC_GENUINE_PARENT_SIGNAL_CAUSALLY_CONFIRMED`

## Identity

| Field | Value |
| --- | --- |
| Signal-ID | `BTCUSDT_20260904T112735Z_eb6191222e_parent` |
| Parent-Event | `BTCUSDT_20260904T112735Z_eb6191222e` |
| Typ | **GENUINE_CROSS_IN** (`trigger_source=CROSS_IN`, `trigger_quality=REAL_CROSS_IN`) |
| UTC | `2026-09-04T11:27:35.764963Z` |
| Lokal (EEST, UTC+3) | `2026-09-04T14:27:35.764963+03:00` |
| Edge | **UPPER / TPO_VAH** @ **81135.0** |
| Triggerpreis | **81292.75** (Abstand **19.41 bps**) |
| u / seq | `4408866` / `805140627861` |

Nicht Bootstrap-Capture, nicht Nested als Parent, nicht Retouch.

Spaetere Nested-Signale im selben Parent (nicht Fokus):
- `BTCUSDT_20260904T112735Z_eb6191222e_ns_3d51be69d9df_1_L` LOWER @ `2026-09-04T11:30:32.654722Z`
- `BTCUSDT_20260904T112735Z_eb6191222e_ns_3d51be69d9df_1_U` UPPER @ `2026-09-04T12:06:34.825323Z`

## Profil

- Fenster: **2026-09-04T10:30:00Z -> 11:00:00Z** (30m)
- Cutoff / kausal: **11:00:00Z** (`trades_strictly_before_cutoff`)
- Basis: Volume-Proxy-Fallback (`tpo_source=volume_proxy_fallback`)
- VAH **81135** / VAL **80965** / POC **80997.5**
- Naechste Kante am Cross: VAH (19.4 bps) vor VAL (~40.3 bps)

## Mechanik (Arm 50 / Entry 20 / Rearm 75)

1. Nach Restart Preis bereits in VAH-Entry-Zone -> `BOOTSTRAP_ALREADY_IN_EDGE_ZONE` (nur Beobachtung).
2. ~11:27:25 Preis verlaesst Entry-Band (APPROACH, ~23 bps).
3. ~11:27:32 Lifecycle **ARMED** (dist <= 50, nicht IN).
4. ~11:27:35.473 letzter APPROACH-Tick mid=81302.75 (~20.63 bps).
5. ~11:27:35.672 Cross-Tick mid=81292.75 (~19.41 bps) -> **CROSS_IN** -> Parent-Capture.

`PROFILE_CAUSALITY_PASS=true`  
`GENUINE_CROSS_PROVEN=true`

## Chart-Kontext (faktisch, 5m)

- Signal in der 5m-Kerze **11:25-11:30 UTC**.
- Kontakt mit **VAH 81135** von oberhalb (Preis ~8129x, Band <=20 bps).
- Unmittelbar vor: ~81302.75; nach: ~81292.75 (weiterhin nahe VAH).
- Lifecycle spaeter: Acceptance -> `FIGHT_ACTIVE` (~11:28:35). Das ist **Ergebnis**, nicht Signalgrund.
- Signalgrund = Preis erreicht Profilkante (Entry-Band). Spaeteres Ergebnis != Signal.

## Capture / Research

- Topic `orderbook.full.BTCUSDT`, eine Parent-Capture, `INITIAL_CHECKPOINT` vor Deltas, Epoch 0.
- Prebuffer actual **~82.1 s** (**< 600 s** — Cold-Start nach Restart).
- Queue-Drops 0, Writer-Fehler 0, Reconnects 0, continuous_capture true, replayable_by_epochs true.
- Isolation-Vertrag `nested_signal_analysis_isolation_v1`, Overlap-Cluster vorhanden.
- `incomplete_reasons`: `NESTED_SIGNAL_QUEUE_DROP` (Nested-Marker-Queue; Parent-Continuity davon getrennt).
- Status: **CURRENTLY_TECHNICALLY_HEALTHY** + **FINAL_RESEARCH_ELIGIBILITY_PENDING** (Event noch offen).
- DB unveraendert (read-only). Collector **1692334** / OI **147111** unveraendert. Kein Commit/Push.
