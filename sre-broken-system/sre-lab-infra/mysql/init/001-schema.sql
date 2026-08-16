-- SRE Lab 使用完全合成数据；任何姓名、邮箱和交易均不对应真实用户。
CREATE DATABASE IF NOT EXISTS sre_lab CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE sre_lab;

-- 用户表支持 user-service 的主键、邮箱和会员查询。
CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    email VARCHAR(255) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    membership_level VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_users_email (email),
    KEY idx_users_status_level (status, membership_level)
) ENGINE=InnoDB;

-- 商品和库存分表，使 inventory/recommendation 具有真实业务数据关系。
CREATE TABLE IF NOT EXISTS products (
    id BIGINT PRIMARY KEY,
    sku VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(64) NOT NULL,
    price DECIMAL(12,2) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE KEY uk_products_sku (sku),
    KEY idx_products_category_active (category, active)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS inventory (
    product_id BIGINT PRIMARY KEY,
    sku VARCHAR(64) NOT NULL,
    available_quantity INT NOT NULL,
    reserved_quantity INT NOT NULL DEFAULT 0,
    version BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_inventory_sku (sku),
    CONSTRAINT fk_inventory_product FOREIGN KEY (product_id) REFERENCES products(id)
) ENGINE=InnoDB;

-- 订单表保留 GOOD 等值查询索引；BAD 代码的前导通配符仍会绕过该 B-Tree。
CREATE TABLE IF NOT EXISTS orders (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id BIGINT NOT NULL,
    customer_email VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL,
    total_amount DECIMAL(12,2) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_orders_customer_email (customer_email),
    KEY idx_orders_user_created (user_id, created_at),
    KEY idx_orders_status_created (status, created_at),
    CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS order_items (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,
    sku VARCHAR(64) NOT NULL,
    quantity INT NOT NULL,
    unit_price DECIMAL(12,2) NOT NULL,
    KEY idx_order_items_order (order_id),
    KEY idx_order_items_product (product_id),
    CONSTRAINT fk_order_items_order FOREIGN KEY (order_id) REFERENCES orders(id),
    CONSTRAINT fk_order_items_product FOREIGN KEY (product_id) REFERENCES products(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS payments (
    id BIGINT PRIMARY KEY,
    order_id BIGINT NOT NULL,
    provider_reference VARCHAR(96) NOT NULL,
    status VARCHAR(32) NOT NULL,
    amount DECIMAL(12,2) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_payments_order (order_id),
    KEY idx_payments_status_created (status, created_at),
    CONSTRAINT fk_payments_order FOREIGN KEY (order_id) REFERENCES orders(id)
) ENGINE=InnoDB;

-- 十位数字辅助表用于集合化生成数据，避免十万次逐行 INSERT。
-- MySQL 不允许在同一查询中多次打开 TEMPORARY TABLE，因此这里短暂创建普通表，
-- 完成全部集合生成后立即删除；DROP IF EXISTS 让上次中断的迁移也可安全重跑。
DROP TABLE IF EXISTS seed_digits;
CREATE TABLE seed_digits (n INT PRIMARY KEY);
INSERT INTO seed_digits VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);

-- 5,000 个用户覆盖多种会员和状态，便于订单创建前的资格判断。
INSERT IGNORE INTO users(id,email,display_name,membership_level,status,created_at)
SELECT sequence_id,
       CONCAT('customer-', sequence_id, IF(MOD(sequence_id,10)=0,'@slow.example.com','@example.com')),
       CONCAT('Synthetic User ', sequence_id),
       ELT(MOD(sequence_id,4)+1,'STANDARD','SILVER','GOLD','PLATINUM'),
       IF(MOD(sequence_id,97)=0,'SUSPENDED','ACTIVE'),
       NOW()-INTERVAL MOD(sequence_id,365) DAY
FROM (
    SELECT 1+d1.n+10*d2.n+100*d3.n+1000*d4.n AS sequence_id
    FROM seed_digits d1 CROSS JOIN seed_digits d2 CROSS JOIN seed_digits d3 CROSS JOIN seed_digits d4
) AS generated_rows WHERE sequence_id<=5000;

-- 兼容第一代实验库：旧 orders 表只有邮箱、状态和金额。下面使用 information_schema
-- 生成幂等 DDL，仅在列、索引或外键缺失时执行 ALTER，因而可以保留已有 100,000 行数据。
SET @ddl = (
    SELECT IF(COUNT(*)=0,
        'ALTER TABLE orders ADD COLUMN user_id BIGINT NULL AFTER id',
        'SELECT 1')
    FROM information_schema.columns
    WHERE table_schema='sre_lab' AND table_name='orders' AND column_name='user_id'
);
PREPARE migration_statement FROM @ddl;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

-- 旧数据按订单主键稳定映射到 5,000 个合成用户；只填空值，不改写新架构产生的数据。
UPDATE orders SET user_id=1+MOD(id-1,5000) WHERE user_id IS NULL;
ALTER TABLE orders MODIFY COLUMN user_id BIGINT NOT NULL;

SET @ddl = (
    SELECT IF(COUNT(*)=0,
        'ALTER TABLE orders ADD INDEX idx_orders_customer_email(customer_email)',
        'SELECT 1')
    FROM information_schema.statistics
    WHERE table_schema='sre_lab' AND table_name='orders' AND index_name='idx_orders_customer_email'
);
PREPARE migration_statement FROM @ddl;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @ddl = (
    SELECT IF(COUNT(*)=0,
        'ALTER TABLE orders ADD INDEX idx_orders_user_created(user_id,created_at)',
        'SELECT 1')
    FROM information_schema.statistics
    WHERE table_schema='sre_lab' AND table_name='orders' AND index_name='idx_orders_user_created'
);
PREPARE migration_statement FROM @ddl;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @ddl = (
    SELECT IF(COUNT(*)=0,
        'ALTER TABLE orders ADD INDEX idx_orders_status_created(status,created_at)',
        'SELECT 1')
    FROM information_schema.statistics
    WHERE table_schema='sre_lab' AND table_name='orders' AND index_name='idx_orders_status_created'
);
PREPARE migration_statement FROM @ddl;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @ddl = (
    SELECT IF(COUNT(*)=0,
        'ALTER TABLE orders ADD CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES users(id)',
        'SELECT 1')
    FROM information_schema.table_constraints
    WHERE table_schema='sre_lab' AND table_name='orders' AND constraint_name='fk_orders_user'
);
PREPARE migration_statement FROM @ddl;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

-- 20,000 个商品及对应库存为推荐扫描和库存预占提供足够基数。
INSERT IGNORE INTO products(id,sku,name,category,price,active)
SELECT sequence_id,CONCAT('SKU-',sequence_id),CONCAT('Synthetic Product ',sequence_id),
       CONCAT('category-',MOD(sequence_id,40)),5+MOD(sequence_id*17,500),TRUE
FROM (
    SELECT 1+d1.n+10*d2.n+100*d3.n+1000*d4.n+10000*d5.n AS sequence_id
    FROM seed_digits d1 CROSS JOIN seed_digits d2 CROSS JOIN seed_digits d3 CROSS JOIN seed_digits d4 CROSS JOIN seed_digits d5
) AS generated_rows WHERE sequence_id<=20000;

INSERT IGNORE INTO inventory(product_id,sku,available_quantity,reserved_quantity,version)
SELECT id,sku,100+MOD(id,50),MOD(id,7),1 FROM products;

-- 100,000 个订单让前导通配符 LIKE 产生稳定、可解释的高 Rows Examined。
INSERT IGNORE INTO orders(id,user_id,customer_email,status,total_amount,created_at)
SELECT sequence_id,1+MOD(sequence_id-1,5000),
       CONCAT('customer-',1+MOD(sequence_id-1,5000),IF(MOD(1+MOD(sequence_id-1,5000),10)=0,'@slow.example.com','@example.com')),
       ELT(MOD(sequence_id,4)+1,'CREATED','PAID','SHIPPED','CANCELLED'),
       10+MOD(sequence_id*137,50000)/100,
       NOW()-INTERVAL MOD(sequence_id,43200) SECOND
FROM (
    SELECT 1+d1.n+10*d2.n+100*d3.n+1000*d4.n+10000*d5.n AS sequence_id
    FROM seed_digits d1 CROSS JOIN seed_digits d2 CROSS JOIN seed_digits d3 CROSS JOIN seed_digits d4 CROSS JOIN seed_digits d5
) AS generated_rows WHERE sequence_id<=100000;

-- 每个订单两条明细，形成 200,000 行 order_items 和真实一对多查询。
INSERT INTO order_items(order_id,product_id,sku,quantity,unit_price)
SELECT orders.id,1+MOD(orders.id*3+item_number,20000),
       CONCAT('SKU-',1+MOD(orders.id*3+item_number,20000)),1+MOD(orders.id+item_number,4),
       5+MOD((orders.id*3+item_number)*17,500)
FROM orders CROSS JOIN (SELECT 1 item_number UNION ALL SELECT 2) item_numbers
WHERE NOT EXISTS (SELECT 1 FROM order_items LIMIT 1);

-- 支付数据取前 50,000 个订单，支持状态和退款诊断。
INSERT IGNORE INTO payments(id,order_id,provider_reference,status,amount,created_at)
SELECT id,id,CONCAT('provider-',id),IF(MOD(id,23)=0,'FAILED','CAPTURED'),total_amount,created_at
FROM orders WHERE id<=50000;

DROP TABLE seed_digits;

-- 业务账号拥有实验库 CRUD；Agent 始终使用只读账号并受 Tool SQL 白名单双重保护。
CREATE USER IF NOT EXISTS 'sre_app'@'%' IDENTIFIED BY 'sre_app_dev_only';
GRANT SELECT,INSERT,UPDATE,DELETE ON sre_lab.* TO 'sre_app'@'%';
CREATE USER IF NOT EXISTS 'sre_reader'@'%' IDENTIFIED BY 'sre_reader_dev_only';
GRANT SELECT,SHOW VIEW,PROCESS ON *.* TO 'sre_reader'@'%';
FLUSH PRIVILEGES;
