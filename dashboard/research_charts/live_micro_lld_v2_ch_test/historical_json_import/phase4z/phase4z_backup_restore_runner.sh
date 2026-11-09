#!/usr/bin/env bash
# Phase 4Z: pre Jun–Sep overnight native five-table backup + isolated restore parity.
set -euo pipefail

RUN_ID="${PHASE4Z_BACKUP_RUN_ID:-phase4z_pre_june_sep_backup}"
REPO_ROOT="/home/telgenbuescher/projects/pools+ob+delta_bot"
LOG_DIR="${REPO_ROOT}/logs/historical_import/overnight_2026"
STATUS_FILE="${LOG_DIR}/${RUN_ID}.exit"
RESULT_FILE="${LOG_DIR}/${RUN_ID}_backup_result.json"
ENV_FILE="/home/telgenbuescher/projects/orderbook_analyse/.env"

mkdir -p "${LOG_DIR}"
: >"${STATUS_FILE}"
trap 'ec=$?; echo "${ec}" >"${STATUS_FILE}"; exit "${ec}"' EXIT

set -a
# shellcheck source=/dev/null
source "${ENV_FILE}"
set +a

BACKUP_TAG="lmv2_pre_june_sep_overnight_2026_$(date -u +%Y%m%dT%H%M%SZ)"
TEST_DB="signal_generator_lmv2_restore_${BACKUP_TAG#lmv2_}"

echo "PHASE4Z_BACKUP_RUN_ID=${RUN_ID}"
echo "BACKUP_TAG=${BACKUP_TAG}"
echo "TEST_DB=${TEST_DB}"
echo "STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

expected_meta=19995
expected_member=4362090
expected_catalog=4358783
expected_publish=20080
expected_sidecar=17935

read -r meta member catalog publish sidecar <<EOF
$(docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "
SELECT
  (SELECT count() FROM signal_generator.research_lmv2_active_snapshot_meta_v1),
  (SELECT count() FROM signal_generator.research_lmv2_active_snapshot_member_v1),
  (SELECT count() FROM signal_generator.research_lmv2_snapshot_active_pool_v1),
  (SELECT count() FROM signal_generator.research_lmv2_active_snapshot_publish_v1),
  (SELECT count() FROM signal_generator.research_lmv2_snapshot_import_provenance_v1)
FORMAT TSV
")
EOF

check_count() {
  local name="$1" actual="$2" expected="$3"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "pre-backup count mismatch ${name}: got ${actual} expected ${expected}" >&2
    exit 2
  fi
}
check_count meta "${meta}" "${expected_meta}"
check_count member "${member}" "${expected_member}"
check_count catalog "${catalog}" "${expected_catalog}"
check_count publish "${publish}" "${expected_publish}"
check_count sidecar "${sidecar}" "${expected_sidecar}"

if docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "SELECT name FROM system.databases WHERE name='${TEST_DB}'" | grep -q .; then
  echo "test database already exists: ${TEST_DB}" >&2
  exit 3
fi

docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "
BACKUP
  TABLE signal_generator.research_lmv2_active_snapshot_meta_v1,
  TABLE signal_generator.research_lmv2_active_snapshot_member_v1,
  TABLE signal_generator.research_lmv2_snapshot_active_pool_v1,
  TABLE signal_generator.research_lmv2_active_snapshot_publish_v1,
  TABLE signal_generator.research_lmv2_snapshot_import_provenance_v1
TO File('backups/${BACKUP_TAG}/lmv2_five_tables')
SETTINGS compression_method='lz4'
"

BACKUP_ID=$(docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "SELECT id FROM system.backups WHERE name LIKE '%${BACKUP_TAG}%' ORDER BY start_time DESC LIMIT 1")

BACKUP_STATUS=$(docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "SELECT status FROM system.backups WHERE id='${BACKUP_ID}'")

if [[ "${BACKUP_STATUS}" != "BACKUP_CREATED" ]]; then
  echo "backup status not BACKUP_CREATED: ${BACKUP_STATUS}" >&2
  exit 4
fi

BACKUP_BYTES=$(docker exec orderbook-clickhouse du -sb \
  "/var/lib/clickhouse/backups/backups/${BACKUP_TAG}/lmv2_five_tables/" 2>/dev/null | awk '{print $1}')

docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "CREATE DATABASE IF NOT EXISTS ${TEST_DB}"

RESTORE_LINE=$(docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "
RESTORE
  TABLE signal_generator.research_lmv2_active_snapshot_meta_v1 AS ${TEST_DB}.research_lmv2_active_snapshot_meta_v1,
  TABLE signal_generator.research_lmv2_active_snapshot_member_v1 AS ${TEST_DB}.research_lmv2_active_snapshot_member_v1,
  TABLE signal_generator.research_lmv2_snapshot_active_pool_v1 AS ${TEST_DB}.research_lmv2_snapshot_active_pool_v1,
  TABLE signal_generator.research_lmv2_active_snapshot_publish_v1 AS ${TEST_DB}.research_lmv2_active_snapshot_publish_v1,
  TABLE signal_generator.research_lmv2_snapshot_import_provenance_v1 AS ${TEST_DB}.research_lmv2_snapshot_import_provenance_v1
FROM File('backups/${BACKUP_TAG}/lmv2_five_tables')
")
RESTORE_ID=$(echo "${RESTORE_LINE}" | awk '{print $1}')
RESTORE_STATUS=$(echo "${RESTORE_LINE}" | awk '{print $2}')

if [[ "${RESTORE_STATUS}" != "RESTORED" ]]; then
  echo "restore status not RESTORED: ${RESTORE_STATUS}" >&2
  exit 5
fi

parity_ok=1
while read -r t; do
  read -r prod restore <<EOF
$(docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "
SELECT
  (SELECT count() FROM signal_generator.${t}),
  (SELECT count() FROM ${TEST_DB}.${t})
FORMAT TSV
")
EOF
  if [[ "${prod}" != "${restore}" ]]; then
    echo "parity fail ${t}: prod=${prod} restore=${restore}" >&2
    parity_ok=0
  fi
done <<'TABLES'
research_lmv2_active_snapshot_meta_v1
research_lmv2_active_snapshot_member_v1
research_lmv2_snapshot_active_pool_v1
research_lmv2_active_snapshot_publish_v1
research_lmv2_snapshot_import_provenance_v1
TABLES

read -r prod_hash restore_hash <<EOF
$(docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "
SELECT
  (SELECT cityHash64(arraySort(groupArray(snapshot_id))) FROM signal_generator.research_lmv2_active_snapshot_meta_v1),
  (SELECT cityHash64(arraySort(groupArray(snapshot_id))) FROM ${TEST_DB}.research_lmv2_active_snapshot_meta_v1)
FORMAT TSV
")
EOF

if [[ "${prod_hash}" != "${restore_hash}" ]]; then
  echo "meta snapshot_id hash mismatch" >&2
  parity_ok=0
fi

read -r meta2 member2 catalog2 publish2 sidecar2 <<EOF
$(docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "
SELECT
  (SELECT count() FROM signal_generator.research_lmv2_active_snapshot_meta_v1),
  (SELECT count() FROM signal_generator.research_lmv2_active_snapshot_member_v1),
  (SELECT count() FROM signal_generator.research_lmv2_snapshot_active_pool_v1),
  (SELECT count() FROM signal_generator.research_lmv2_active_snapshot_publish_v1),
  (SELECT count() FROM signal_generator.research_lmv2_snapshot_import_provenance_v1)
FORMAT TSV
")
EOF

if [[ "${meta}" != "${meta2}" || "${member}" != "${member2}" ]]; then
  echo "production changed during backup/restore test" >&2
  exit 6
fi

if [[ "${parity_ok}" != "1" ]]; then
  exit 7
fi

python3 - <<PY
import json
from pathlib import Path
out = Path("${RESULT_FILE}")
out.write_text(json.dumps({
    "run_id": "${RUN_ID}",
    "backup_tag": "${BACKUP_TAG}",
    "backup_id": "${BACKUP_ID}",
    "backup_status": "${BACKUP_STATUS}",
    "backup_bytes": int("${BACKUP_BYTES:-0}"),
    "backup_name": "File('backups/${BACKUP_TAG}/lmv2_five_tables')",
    "restore_id": "${RESTORE_ID}",
    "restore_status": "${RESTORE_STATUS}",
    "restore_test_database": "${TEST_DB}",
    "pre_post_prod_counts": {
        "meta": int("${meta}"),
        "member": int("${member}"),
        "catalog": int("${catalog}"),
        "publish": int("${publish}"),
        "sidecar": int("${sidecar}"),
    },
    "meta_snapshot_hash_sorted": "${prod_hash}",
    "finished_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
}, indent=2), encoding="utf-8")
print(out)
PY

echo "BACKUP_ID=${BACKUP_ID}"
echo "FINISHED_OK"
