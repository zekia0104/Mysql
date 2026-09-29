-- 2026_09_26_0002__add_orders_channel.sql
--
-- The same intent as the bad example next to this one, written so that no
-- statement rebuilds a table, blocks writes, or waits a year for a metadata
-- lock. This file is the target: `migration-guard examples/good` exits 0.

-- Every ALTER below needs an exclusive metadata lock at the start and end. The
-- default wait is 31536000 seconds, so fail fast and let the runner retry.
SET SESSION lock_wait_timeout = 5;

-- One ALTER, not five: the table is touched once, so it takes one metadata lock
-- and one rebuild check instead of five of each.
--
--   * the column is nullable with no default, so it can be added to a populated
--     table (existing rows get NULL, which is what "unknown" should mean here);
--   * INSTANT is stated so that MySQL errors out rather than falling back to a
--     rebuild if the storage engine cannot do it in place;
--   * MySQL 8.0.29 and later add a column anywhere without touching the rows.
ALTER TABLE orders
    ADD COLUMN channel VARCHAR(32) NULL,
    ALGORITHM=INSTANT;

-- Backfill in batches. Run this separately, not inside the migration, and loop
-- until the affected-row count is zero:
--
--   UPDATE orders SET channel = 'unknown'
--    WHERE channel IS NULL
--    ORDER BY id
--    LIMIT 5000;
--
-- Only after every row has a value is it safe to tighten the column:
--
--   ALTER TABLE orders MODIFY COLUMN channel VARCHAR(32) NOT NULL DEFAULT 'unknown';
--
-- That last statement rebuilds the table, so schedule it as an online change
-- (gh-ost, pt-online-schema-change) rather than running it here.

-- An index needed by the reporting queries. ADD INDEX is in-place and allows
-- concurrent writes, but stating LOCK=NONE turns a silent fallback into an error.
ALTER TABLE orders
    ADD INDEX idx_customer_created (customer_id, created_at),
    ALGORITHM=INPLACE, LOCK=NONE;

-- Removing an index is cheap for the schema. It is only safe once something has
-- confirmed nothing reads it -- check
-- performance_schema.table_io_waits_summary_by_index_usage first.
--
-- ALTER TABLE orders DROP INDEX idx_obsolete, ALGORITHM=INPLACE, LOCK=NONE;

-- To remove a table, rename it out of the way and drop it a release later; a
-- dropped table cannot be brought back by rolling the migration back.
--
-- RENAME TABLE legacy_order_archive TO zz_removed_2026_09_26;
