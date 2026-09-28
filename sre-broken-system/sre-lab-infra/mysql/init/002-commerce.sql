-- Additive migration: legacy demonstration data is preserved.
CREATE DATABASE IF NOT EXISTS payment_db CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS payment_db.payments (
 id VARCHAR(36) PRIMARY KEY,
 order_id BIGINT NOT NULL UNIQUE,
 amount DECIMAL(14,2) NOT NULL,
 status VARCHAR(32) NOT NULL,
 idempotency_key VARCHAR(128) NOT NULL UNIQUE,
 created_at DATETIME(3) NOT NULL
);
CREATE USER IF NOT EXISTS 'payment_app'@'%' IDENTIFIED BY 'payment_app_dev_only';
GRANT SELECT,INSERT,UPDATE ON payment_db.* TO 'payment_app'@'%';
