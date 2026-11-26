# MC-POOL-P9 — Restore from local backup

1. Backup root: `/home/telgenbuescher/backups/mc-pool-p9_20261126/`
2. Verify: `sha256sum -c checksums/archives.sha256`
3. Extract spread: `tar -xzf archives/spread_recovery_hedge_short_dev.tar.gz -C /home/telgenbuescher/projects`
4. Git recovery: checkout branch `recovery/mc-pool-p9-20261126` or tag `stable-mc-pool-p9-20261126`
5. systemd: copy from `systemd/` to `~/.config/systemd/user/` if needed (no restart in P9 runbook unless operator chooses)
6. ClickHouse / collector / OB runtime state **not** in archive — restore separately.