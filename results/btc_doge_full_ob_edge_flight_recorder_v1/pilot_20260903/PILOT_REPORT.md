# Shadow-Pilot Report — Full-OB Edge Flight Recorder

**UTC window:** 2026-09-03T18:41:35Z (pre) → restart ~18:42:12Z → observe 18:42:44Z–18:57:14Z (30×30s = 15 min)

## Verdict

`SHADOW_PILOT_LIVE_CAPTURE_OK_CHART_FULL_SOCKET_BLOCKED_WHILE_CAPTURING`

Collector-Restart einmalig ausgeführt. BTC/DOGE Full-OB `book_ready`, `u`-kontinuierlich, 0 Gaps/Resyncs, je ein Event, Replay der offenen Pakete ok.  
**Grenze:** Unix-Socket `depth=0` (FULL snapshot/status) timeout während Capture — wahrscheinlich Lock+Sync-Write im Ingest. OB1000-Socket antwortet. Dashboard-PID unverändert. **Kein zweiter Restart.**

Kein Commit, kein Push, keine Tradingaktionen, keine DB-Writes.

## PIDs

| Rolle | Alt | Neu | Status |
|---|---|---|---|
| Full-OB/OB200 Collector `raw-archive-only BTCUSDT,DOGEUSDT` | **1388502** | **1467869** | LIVE |
| OI/Liquidation Collector | 147111 | 147111 | unverändert |
| Dashboard API :3000 | 1438248 | 1438248 | unverändert |

Genau **ein** `orderbook_v2_live --mode raw-archive-only`. Confirmed topics: `orderbook.200.*` + `orderbook.full.*` (je BTC/DOGE) — keine zweite Full-OB-Verbindung.

## Env

```
OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE=true
OB_V3_FULL_OB_FR_SYMBOLS=BTCUSDT,DOGEUSDT
OB_V3_FULL_OB_FR_ROOT=/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_edge_flight_recorder
OB_V3_FULL_BOOK_ENABLE=true
OB_V3_ON_DEMAND_ENABLE=true
OB_V3_RAW_ARCHIVE_SYMBOLS=BTCUSDT,DOGEUSDT
```

Restart: SIGTERM 1388502 → `scripts/start_orderbook_v3_raw_archive_btc_doge.sh` mit explizitem `FR_ENABLE=true`.

## book_ready / Sync (t15)

| Symbol | WS | REST+Align | book_ready | u start→end (Δ) | seq Δ | gaps | reconnects | bids | asks |
|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | live | ja | **true** | 4107378→4111759 (**+4381**) | +6 031 628 | **0** | **0** | ~41061 | ~21624 |
| DOGEUSDT | live | live | **true** | 4107065→4111445 (**+4380**) | +4 833 423 | **0** | **0** | ~5490 | ~13308 |

`u` ≈ 4.87/s (Bybit Full-OB 200 ms). `seq` steigt (nicht consecutive — Bybit-Vertrag). Replay: 2 stale/dup/dec-seq verworfen, **0 Gaps**.

## Keeper / Recorder

- `full_book_active_topics=2` ohne Chart (OB1000 `subscription_state=stopped`, `on_demand_leases=[]`).
- Recorder: beide Symbole `FIGHT_ACTIVE` (ein Event je Symbol, IDs unverändert über 15 min → **kein Doppel-Event**).
- Synthetischer Watcher-Test **nicht nötig** — echte Capture sofort nach Ready.

Events (noch offen, max 90 min / Fight):

- `.../BTCUSDT/2026-09-03/BTCUSDT_20260903T184212Z_4a22a89fe6/`
- `.../DOGEUSDT/2026-09-03/DOGEUSDT_20260903T184212Z_f5d68293cd/`

Nur unter `full_ob_edge_flight_recorder/` (tmp während Capture).

## Replay / SHA (Kopie der offenen Streams)

Manifest noch nicht final (Capture läuft). Offline-Replay der kopierten `.tmp`:

| | BTCUSDT | DOGEUSDT |
|---|---|---|
| snapshot SHA256 | `bebefd72…c19617` | `123ff0a1…0c8ab` |
| deltas SHA256 | `c6e9ae54…679a3d` | `ef6e1921…ae941` |
| snap u / seq | 4107241 / 804699175536 | 4106933 / 345785498584 |
| snap levels | 40781 / 22017 | 5456 / 13343 |
| applied deltas | 4783 | 4370 |
| gaps | 0 | 0 |
| stale ignored | 2 | 2 |
| crossed | false | false |
| **replay_ok** | **true** | **true** |

Detail: `pilot_20260903/replay_open_events.json`

## Ressourcen

| | Pre (alt PID) | t0 ~28s | t15 |
|---|---|---|---|
| RSS | 115 MB | 165–170 MB | **~190 MB** |
| CPU | 1.8 % | 4.8 % | **~3.8 %** |

Netz (`/proc/1467869/io` nach ~16 min): rchar ~21 MB, write_bytes ~10.8 MB.

Speicher FR-Root nach ~16 min: **~5.0 MB** (beide Events capturing).  
Falls **dauerhaft** beide kämpfen: ~5 MB / 16 min × 1440 min ≈ **~450 MB/Tag** komprimiert.  
Bei selteneren Kanten deutlich weniger. Disk frei ~356 GB.

## Chart

- Dashboard-Prozess nicht angefasst.
- OB1000-Unix-Socket: **OK** (~0.2 s).
- Full-OB-Unix-Socket `depth=0` status/snapshot: **Timeout 2 s** während Capture.

Ursache (befundet, nicht gefixt): Observer schreibt Deltas **innerhalb** `_book_lock` → Socket-Handler blockiert. Nächster Fix: Notify/Write außerhalb des Book-Locks. Dafür wäre ein zweiter Restart nötig.

## Verbleibende Grenzen

- Live-`publicTrade`-WS nicht mitgeschrieben (leere `public_trades_raw.jsonl.zst.tmp`).
- Events noch nicht atomar finalisiert (Fight aktiv).
- RPI nicht in Full-OB.
- Full-Chart-Snapshot während Capture blockiert (s.o.).
- Health listet Full-OB-Lease-IDs nicht explizit (Keeper nur über Topics/active_topics belegt).

## Artefakte

`results/btc_doge_full_ob_edge_flight_recorder_v1/pilot_20260903/`
- `pre_restart.json`, `pids.txt`, `new_process_env.txt`
- `health_samples/`, `health_t15.json`, `resource_samples/`
- `copied_open_events/`, `replay_open_events.json`
- Live-Capture: `orderbook_analyse/data/orderbook_raw_shadow/full_ob_edge_flight_recorder/`
