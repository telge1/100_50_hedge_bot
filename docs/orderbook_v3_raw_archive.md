# Orderbook V3 Raw OB200 Live Archive

Parallel, loss-aware raw orderbook archival for the existing `OrderbookV3LiveCollector`.

## Architecture

```
Bybit orderbook.200 WebSocket
        │
        ├─ BookState + LiveSecondClock
        │      └─ FeatureWriter → orderbook_features_1s_v2
        │
        └─ RawArchiveManager (bounded asyncio queue)
               └─ SegmentWriter (zstd NDJSON)
                      └─ data/orderbook_raw_live/ob200_v3/<SYMBOL>/YYYY/MM/DD/
```

The aggregate pipeline is unchanged. Raw archival branches at `_ingest_ready()` in
`collector.py` **before** `LiveSecondClock.ingest()` mutates `BookState`.

## Event format

Market events preserve native Bybit WS JSON (compatible with `parse_ob200_obj` / `OrderBookReplayer`):

```json
{
  "topic": "orderbook.200.BTCUSDT",
  "type": "snapshot",
  "ts": 1750000000010,
  "cts": null,
  "data": {"s": "BTCUSDT", "b": [["90000","5"]], "a": [["90001","4"]], "u": 1, "seq": 100},
  "format_version": "ob200_v3_live_archive/v1",
  "parser_version": "ob200_v3",
  "depth": 200,
  "local_receive_ts": "2025-08-20T12:00:00Z"
}
```

Lifecycle markers use `archive_event` (skipped by replayer). Rotation checkpoints use
`type=rotation_checkpoint`, `source=local_book_state` and replay as snapshots.

## Configuration (disabled by default)

| Env | Default | Description |
|-----|---------|-------------|
| `OB_V3_RAW_ARCHIVE_ENABLE` | `false` | Master switch |
| `OB_V3_RAW_ARCHIVE_ROOT` | `data/orderbook_raw_live/ob200_v3` | Archive root |
| `OB_V3_RAW_ARCHIVE_SYMBOLS` | *(empty)* | Comma-separated symbols, e.g. `BTCUSDT` |
| `OB_V3_RAW_ARCHIVE_QUEUE_SIZE` | `8192` | Bounded queue |
| `OB_V3_RAW_ARCHIVE_ROTATION` | `day` | `day` or `hour` |
| `OB_V3_RAW_ARCHIVE_COMPRESSION` | `zstd` | `zstd` or `none` |
| `OB_V3_RAW_ARCHIVE_COMPRESSION_LEVEL` | `3` | zstd level |
| `OB_V3_RAW_ARCHIVE_RETENTION_DAYS` | `0` | Dry-run only (no auto-delete) |
| `OB_V3_RAW_ARCHIVE_MIN_FREE_DISK_GB` | `5` | Pause archival below this |
| `OB_V3_RAW_ARCHIVE_WARN_FREE_DISK_GB` | `20` | Warn threshold |

## Queue / overflow

- `try_enqueue_market()` is non-blocking (`put_nowait`).
- On queue full or disk below minimum: aggregate collector continues; segment marked
  non-replayable; `QUEUE_OVERFLOW` marker written when possible.

## Rotation / checkpoints

Segments rotate on UTC day (or hour). New segments can begin with `ROTATION_CHECKPOINT`
from current valid `BookState` for independent replay.

## BTC raw archive start (safe, parallel to universe51 collector)

Use **`--mode raw-archive-only`** — not `shadow3 --skip-lock`. Archive-only uses a
dedicated lock (`logs/orderbook_v3_raw_archive_only.lock`), writes no ClickHouse
features, and does not share the live collector health identity.

```bash
OB_V3_RAW_ARCHIVE_ENABLE=true \
OB_V3_RAW_ARCHIVE_SYMBOLS=BTCUSDT \
OB_V3_RAW_ARCHIVE_ROOT=/data/orderbook_raw_btc_shadow/ob200_v3 \
OB_V3_RAW_ARCHIVE_ROTATION=hour \
OB_V3_RAW_ARCHIVE_RETENTION_DAYS=0 \
OB_V3_RAW_ARCHIVE_WARN_FREE_DISK_GB=20 \
OB_V3_RAW_ARCHIVE_MIN_FREE_DISK_GB=5 \
nohup env PYTHONPATH=src python -m orderbook_analyse.orderbook_v2_live \
  --mode raw-archive-only \
  --symbols BTCUSDT \
  --health-file logs/orderbook_v3_raw_archive_btc.health.ndjson \
  --log-level INFO \
  >> logs/orderbook_v3_raw_archive_btc.nohup.log 2>&1 &
```

Do **not** use `--skip-lock`. The existing 51-coin aggregation collector and OI/Liq
collector remain unchanged.

### Why not `shadow3 --skip-lock`?

`shadow3` always subscribes ADAUSDT,BTCUSDT,ETHUSDT, runs `LiveSecondClock`, and writes
1s aggregates to `orderbook_features_1s_v2`. Raw archive env vars only filter which
symbols are archived — they do not disable the feature pipeline.

## Offline smoke

```bash
PYTHONPATH=src python scripts/run_orderbook_v3_raw_archive_offline_smoke.py
```

Outputs: `results/orderbook_v3_raw_archive/offline_smoke/`

## Storage estimate

Rough order of magnitude for BTCUSDT OB200 at ~1–5 Hz mixed snapshot/delta:
~50–200 MB/day compressed (zstd level 3), highly variable with volatility.
