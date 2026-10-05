# Live scanner processes

Long and short dry-run scanners are started manually (no systemd unit in this repo):

```bash
cd /home/telgenbuescher/projects/pools+ob+delta_bot
nohup python -m bot.e1r_live_scanner.runner </dev/null >> bot/e1r_live_scanner/logs/runner_live.log 2>&1 &
nohup python -m bot.long_v1_live_scanner.runner </dev/null >> bot/long_v1_live_scanner/logs/runner_live.log 2>&1 &
```

`SHADOW_CH_SYNC_ENABLED` is set automatically for `live=True` via `apply_live_scanner_runtime_env()` in each runner (default `1`; override via process env or collector `.env`).

Do not commit `runtime/`, `logs/`, or registry JSONL snapshots.
