# Stale Capture Recovery Report

**UTC:** 2026-09-04T19:37:52Z

## Summary

- **6** orphan `*.tmp` files across **2** event directories (BTC+DOGE `20260903T184212Z_*`)
- Size/mtime stable; no live FD holders
- Original SHA256 recorded; **bit-identical working copies** under `stale_tmp_workdir_copies/`
- Originals **not** modified
- Marked `INCOMPLETE_AT_PROCESS_STOP`
- **research_eligible=false**
- Do **not** continue these events with a new collector process
- Full-OB ClickHouse importer remains **disabled/absent**

## Events with TMP

See `stale_capture_inventory.csv` and `stale_tmp_sha256.json`.

Other event directories under FR root appear finalized/without open `.tmp` (51 additional dirs inventoried without tmp).
