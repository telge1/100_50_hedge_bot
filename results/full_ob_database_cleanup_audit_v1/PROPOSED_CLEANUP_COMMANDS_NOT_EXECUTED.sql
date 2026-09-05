-- PROPOSED CLEANUP COMMANDS — NOT EXECUTED
-- Generated: 2026-09-04T11:13:31.399553Z
-- Audit verdict: FULL_OB_DATABASE_CLEAN_PRODUCTION_SMOKE_ISOLATED
-- DO NOT RUN without separate explicit approval.

-- 1) Optional: remove idempotency demo table only
-- DROP TABLE IF EXISTS research_full_ob_smoke.full_ob_gap_audit_packets_v1_dedupdemo;

-- 2) Optional: after export/backup, drop entire isolated smoke database
-- DROP VIEW IF EXISTS research_full_ob_smoke.v_full_ob_gap_audit_packets_dedup;
-- DROP TABLE IF EXISTS research_full_ob_smoke.full_ob_level_changes_smoke_v1;
-- DROP TABLE IF EXISTS research_full_ob_smoke.full_ob_packets_smoke_v1;
-- DROP TABLE IF EXISTS research_full_ob_smoke.full_ob_gap_audit_packets_v1;
-- DROP TABLE IF EXISTS research_full_ob_smoke.full_ob_multi_epoch_packets_v1;
-- DROP DATABASE IF EXISTS research_full_ob_smoke;

-- 3) NEVER propose deleting JSONL.zst / rest snapshots / event manifests
-- 4) NEVER mutate open .tmp files in this change window
-- 5) No TRUNCATE / ALTER DELETE / OPTIMIZE proposed for production
-- 6) orderbook_analysis.orderbook_deltas broken-parts repair is OUT OF SCOPE (pre-existing; separate ops ticket)

-- Event referenced by smoke: BTCUSDT_20260904T080534Z_1fd9a66d36
