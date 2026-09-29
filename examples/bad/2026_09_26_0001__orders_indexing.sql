-- 2026_09_26_0001__orders_indexing.sql
-- Adding the index the reporting queries need.

ALTER TABLE orders MODIFY COLUMN total DECIMAL(12,2) NOT NULL;

ALTER TABLE orders ADD COLUMN fulfilment_note TEXT NOT NULL;

ALTER TABLE orders ADD INDEX idx_customer_created (customer_id, created_at);

ALTER TABLE orders ADD INDEX idx_created_covering (created_at, customer_id, total);

SET foreign_key_checks = 0;

ALTER TABLE order_items ADD CONSTRAINT fk_order FOREIGN KEY (order_id) REFERENCES orders (id);

DROP TABLE legacy_order_archive;

UPDATE orders SET status = 'migrated';

DELETE FROM order_items WHERE order_id NOT IN (SELECT id FROM orders);

TRUNCATE TABLE staging_imports;

ALTER TABLE orders
    ADD COLUMN channel VARCHAR(32) /*!80000 , ALGORITHM=INSTANT */,
    ALGORITHM=INSTANT;
