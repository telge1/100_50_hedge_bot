#!/usr/bin/env python3
"""20-minute smoke sampler for spool-meta-fix live restart."""
from __future__ import annotations
import csv, json, os, time, subprocess
from datetime import datetime, timezone
from pathlib import Path
import clickhouse_connect
from dotenv import load_dotenv

OA = Path('/home/telgenbuescher/projects/orderbook_analyse')
ART = Path('/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/oi_liquidation_spool_meta_race_live_restart_v1')
load_dotenv(OA / '.env')
c = clickhouse_connect.get_client(
    host=os.environ['CLICKHOUSE_HOST'],
    port=int(os.environ.get('CLICKHOUSE_HTTP_PORT', '8123')),
    username=os.environ['CLICKHOUSE_USER'],
    password=os.environ['CLICKHOUSE_PASSWORD'],
    database=os.environ.get('CLICKHOUSE_DATABASE', 'default'),
)

# Sample at relative minutes from start: 1,5,10,15,20 plus every 60s fill
targets = {1, 5, 10, 15, 20}
# Also sample every minute for denser series
duration_min = 20
start = time.time()
start_nre = int(subprocess.check_output(
    ['systemctl', '--user', 'show', '-p', 'NRestarts', '--value', 'bybit-oi-liquidation-collector.service'],
    text=True).strip() or '0')
before = json.loads((ART / 'clickhouse_before.json').read_text())
dup_cutoff = before['duplicate_baseline_cutoff_utc']

hs_path = ART / 'health_samples.csv'
meta_path = ART / 'spool_meta_progress.csv'
parity_path = ART / 'oi_source_spool_db_parity.csv'
log_path = ART / 'smoke_sampler.log'

hs_fields = [
    'sample_i','utc','t_plus_min','pid','instances','nrestarts_delta','health_status',
    'websocket_alive','writer_alive','clickhouse_reachable',
    'last_ws_message_ts','last_oi_received_ts','last_oi_persisted_ts','last_successful_insert_ts',
    'persistence_lag_seconds','queue_depth','queue_drop_count','writer_error_count',
    'spool_unacked_records','spool_unacked_bytes','clickhouse_reconnect_count','websocket_reconnect_count',
    'meta_generation','meta_last_acked','meta_next_seq','close_wait','session_locked_post',
    'meta_enoent_post','insert_attempt_post','ack_attempt_post','is_gate_point',
]
meta_fields = ['sample_i','utc','generation','last_acked_seq','next_seq','unacked_health','orphan_tmps']
parity_fields = [
    'sample_i','utc','oi5s_count','oi5s_max','oie_count','oie_max',
    'btc_max','doge_max','eth_max','sol_max','xrp_max',
    'oie_phys_since_restart','oie_uniq_since_restart','oie_extra_since_restart',
    'liq_count','liq_max','liq_phys_since_restart','liq_uniq_since_restart',
]

for p, fields in [(hs_path, hs_fields), (meta_path, meta_fields), (parity_path, parity_fields)]:
    with p.open('w', newline='') as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()

def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line, flush=True)
    with log_path.open('a') as f:
        f.write(line + '\n')

# mark restart boundary in log search
restart_marker = '2026-09-04 20:45'

def post_log_counts():
    text = (OA / 'logs/oi_liquidation_live.log').read_text(errors='replace')
    idx = text.rfind(restart_marker)
    post = text[idx:] if idx >= 0 else ''
    return {
        'session_locked_post': post.count('SESSION_IS_LOCKED'),
        'meta_enoent_post': post.count("No such file or directory") and post.count('meta.json.tmp'),
        'meta_enoent_old_path': post.count('/spool/meta.json.tmp\'') + post.count('/spool/meta.json.tmp"'),
        'insert_attempt_post': post.count('insert attempt'),
        'ack_attempt_post': post.count('spool ack attempt'),
    }

def sample(i: int, t_plus: float, gate: bool):
    pid = int(subprocess.check_output(
        ['systemctl', '--user', 'show', '-p', 'MainPID', '--value', 'bybit-oi-liquidation-collector.service'],
        text=True).strip())
    inst = int(subprocess.check_output(
        ['bash', '-lc', "pgrep -af 'python -m orderbook_analyse.oi_liquidation_collector' | grep -v grep | wc -l"],
        text=True).strip() or '0')
    nre = int(subprocess.check_output(
        ['systemctl', '--user', 'show', '-p', 'NRestarts', '--value', 'bybit-oi-liquidation-collector.service'],
        text=True).strip() or '0')
    h = json.loads((OA / 'logs/oi_liquidation_collector.health.json').read_text())
    meta = json.loads((OA / 'data/oi_liquidation_collector/spool/meta.json').read_text())
    orphans = list((OA / 'data/oi_liquidation_collector/spool').glob('meta.json.tmp*'))
    cw = int(subprocess.check_output(
        ['bash', '-lc', f"ss -antp 2>/dev/null | awk '$1==\"CLOSE-WAIT\" && index($0,\"pid={pid}\")>0' | wc -l"],
        text=True).strip() or '0')
    counts = post_log_counts()
    # CH
    oi5s = c.query('SELECT count(), max(bucket_time) FROM open_interest_5s').result_rows[0]
    oie = c.query('SELECT count(), max(event_time) FROM open_interest_events').result_rows[0]
    liq = c.query('SELECT count(), max(event_time) FROM all_liquidations').result_rows[0]
    syms = {}
    for s in ['BTCUSDT','DOGEUSDT','ETHUSDT','SOLUSDT','XRPUSDT']:
        syms[s] = str(c.query(f"SELECT max(bucket_time) FROM open_interest_5s WHERE symbol='{s}'").result_rows[0][0])
    # duplicates since THIS restart (use received_at >= dup_cutoff from before file — actually use process start)
    # Prefer health updated / use cutoff from clickhouse_before duplicate_baseline_cutoff_utc
    cut = dup_cutoff
    oie_d = c.query(
        f"SELECT count(), uniqExact(event_key) FROM open_interest_events WHERE received_at >= toDateTime64('{cut}',3)"
    ).result_rows[0]
    liq_d = c.query(
        f"SELECT count(), uniqExact(event_key) FROM all_liquidations WHERE received_at >= toDateTime64('{cut}',3)"
    ).result_rows[0]

    utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    lag = h.get('persistence_lag_seconds')
    if lag is None:
        lo = h.get('last_oi_received_ts') or 0
        lp = h.get('last_oi_persisted_ts') or 0
        lag = max(0.0, float(lo) - float(lp)) if lo and lp else 0.0

    hs = {
        'sample_i': i, 'utc': utc, 't_plus_min': round(t_plus, 3), 'pid': pid, 'instances': inst,
        'nrestarts_delta': nre - start_nre,
        'health_status': h.get('health_status'),
        'websocket_alive': h.get('websocket_alive'),
        'writer_alive': h.get('writer_alive'),
        'clickhouse_reachable': h.get('clickhouse_reachable'),
        'last_ws_message_ts': h.get('last_ws_message_ts'),
        'last_oi_received_ts': h.get('last_oi_received_ts'),
        'last_oi_persisted_ts': h.get('last_oi_persisted_ts'),
        'last_successful_insert_ts': h.get('last_successful_insert_ts'),
        'persistence_lag_seconds': lag,
        'queue_depth': h.get('queue_depth'),
        'queue_drop_count': h.get('queue_drop_count'),
        'writer_error_count': h.get('writer_error_count'),
        'spool_unacked_records': h.get('spool_unacked_records'),
        'spool_unacked_bytes': h.get('spool_unacked_bytes'),
        'clickhouse_reconnect_count': h.get('clickhouse_reconnect_count'),
        'websocket_reconnect_count': h.get('websocket_reconnect_count'),
        'meta_generation': meta.get('generation'),
        'meta_last_acked': meta.get('last_acked_seq'),
        'meta_next_seq': meta.get('next_seq'),
        'close_wait': cw,
        'session_locked_post': counts['session_locked_post'],
        'meta_enoent_post': counts['meta_enoent_old_path'],
        'insert_attempt_post': counts['insert_attempt_post'],
        'ack_attempt_post': counts['ack_attempt_post'],
        'is_gate_point': gate,
    }
    with hs_path.open('a', newline='') as f:
        csv.DictWriter(f, fieldnames=hs_fields).writerow(hs)
    with meta_path.open('a', newline='') as f:
        csv.DictWriter(f, fieldnames=meta_fields).writerow({
            'sample_i': i, 'utc': utc, 'generation': meta.get('generation'),
            'last_acked_seq': meta.get('last_acked_seq'), 'next_seq': meta.get('next_seq'),
            'unacked_health': h.get('spool_unacked_records'), 'orphan_tmps': len(orphans),
        })
    with parity_path.open('a', newline='') as f:
        csv.DictWriter(f, fieldnames=parity_fields).writerow({
            'sample_i': i, 'utc': utc,
            'oi5s_count': oi5s[0], 'oi5s_max': oi5s[1],
            'oie_count': oie[0], 'oie_max': oie[1],
            'btc_max': syms['BTCUSDT'], 'doge_max': syms['DOGEUSDT'],
            'eth_max': syms['ETHUSDT'], 'sol_max': syms['SOLUSDT'], 'xrp_max': syms['XRPUSDT'],
            'oie_phys_since_restart': oie_d[0], 'oie_uniq_since_restart': oie_d[1],
            'oie_extra_since_restart': oie_d[0] - oie_d[1],
            'liq_count': liq[0], 'liq_max': liq[1],
            'liq_phys_since_restart': liq_d[0], 'liq_uniq_since_restart': liq_d[1],
        })
    log(f"sample {i} t+{t_plus:.1f}m gate={gate} status={h.get('health_status')} ws={h.get('websocket_alive')} "
        f"werr={h.get('writer_error_count')} drops={h.get('queue_drop_count')} oi5s_max={oi5s[1]} "
        f"extra={oie_d[0]-oie_d[1]} gen={meta.get('generation')} acked={meta.get('last_acked_seq')} cw={cw}")

i = 0
# Sample approximately every 60s for 20 minutes; mark required gate minutes.
next_min = 0
while next_min <= duration_min:
    # wait until target minute
    while (time.time() - start) / 60.0 < next_min:
        time.sleep(0.5)
    elapsed_min = (time.time() - start) / 60.0
    gate = int(round(next_min)) in targets or next_min in targets
    sample(i, elapsed_min, gate)
    i += 1
    next_min += 1

log('SMOKE_SAMPLER_DONE')
print('SMOKE_SAMPLER_DONE')
