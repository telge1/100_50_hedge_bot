-- Full-OB smoke analysis examples against research_full_ob_smoke

-- 1) All changes at an exact price level
SELECT update_id, seq, action, quantity, exchange_ts_ms
FROM research_full_ob_smoke.full_ob_level_changes_smoke_v1 FINAL
WHERE side = 'bid' AND price = '80653.6'
ORDER BY update_id;

-- 2) Bid/Ask quantity activity in a price range over time
SELECT
  intDiv(exchange_ts_ms, 10000) AS bucket_10s,
  side,
  count() AS changes,
  countIf(action = 'DELETE') AS deletes,
  countIf(action = 'UPSERT') AS upserts
FROM research_full_ob_smoke.full_ob_level_changes_smoke_v1 FINAL
WHERE toFloat64OrZero(price) BETWEEN 80000 AND 82000
GROUP BY bucket_10s, side
ORDER BY bucket_10s, side;

-- 3) Deletes with quantity = 0
SELECT count() FROM research_full_ob_smoke.full_ob_level_changes_smoke_v1 FINAL WHERE action = 'DELETE' AND quantity = '0';

-- 4) Refills after a prior delete on same price
SELECT price, countIf(action = 'DELETE') AS dels, countIf(action = 'UPSERT') AS ups
FROM research_full_ob_smoke.full_ob_level_changes_smoke_v1 FINAL
WHERE side = 'ask'
GROUP BY price
HAVING dels >= 1 AND ups >= 1
ORDER BY ups DESC
LIMIT 20;

-- 5) Largest quantity upserts in the smoke window
SELECT update_id, side, price, quantity, action
FROM research_full_ob_smoke.full_ob_level_changes_smoke_v1 FINAL
WHERE action = 'UPSERT'
ORDER BY abs(toFloat64OrZero(quantity)) DESC
LIMIT 50;

-- 6) Packets immediately before/after a selected u
SELECT message_type, update_id, seq, exchange_ts_ms, length(bids), length(asks)
FROM research_full_ob_smoke.full_ob_packets_smoke_v1 FINAL
WHERE message_type = 'delta' AND update_id BETWEEN {u} - 2 AND {u} + 2
ORDER BY update_id, source_line_number;

-- 7) End-checkpoint packet aggregates (best bid/ask & level counts come from replay parity)
SELECT
  countIf(message_type = 'delta') AS delta_packets,
  min(update_id) AS min_u,
  max(update_id) AS max_u,
  min(seq) AS min_seq,
  max(seq) AS max_seq
FROM research_full_ob_smoke.full_ob_packets_smoke_v1 FINAL;
