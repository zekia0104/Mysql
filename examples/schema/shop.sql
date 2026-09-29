-- shop schema, as `mysqldump --no-data --skip-comments --compact shop` writes it.
--
-- Passing this to the guard enables the rules that need to know what is already
-- there: adding an index the table already has, indexing an unbounded TEXT
-- column, narrowing a column past what it holds, or naming a column that does
-- not exist. Without it those rules stay silent rather than guessing.
--
--     migration-guard --schema examples/schema/shop.sql examples/bad

CREATE TABLE `customers` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `email` VARCHAR(255) NOT NULL,
  `display_name` VARCHAR(120) DEFAULT NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_customers_email` (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `orders` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `customer_id` BIGINT NOT NULL,
  `status` VARCHAR(32) NOT NULL DEFAULT 'new',
  `total` DECIMAL(12,2) NOT NULL DEFAULT '0.00',
  `currency` CHAR(3) NOT NULL DEFAULT 'CNY',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `internal_notes` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_orders_customer` (`customer_id`),
  KEY `idx_orders_status` (`status`),
  CONSTRAINT `fk_orders_customer` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `order_items` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `order_id` BIGINT NOT NULL,
  `sku` VARCHAR(64) NOT NULL,
  `quantity` SMALLINT NOT NULL DEFAULT '1',
  `unit_price` DECIMAL(10,2) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_order_items_order` (`order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `legacy_order_archive` (
  `id` BIGINT NOT NULL,
  `payload` MEDIUMTEXT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `staging_imports` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `batch` VARCHAR(40) NOT NULL,
  `raw` TEXT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
