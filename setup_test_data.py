"""Create the two sample databases with deliberate, known differences.

    python setup_test_data.py            # drop and recreate both databases

Differences planted on purpose
-----------------------------
SCHEMA
  products        exists only in A
  inventory       exists only in B
  customers       B adds loyalty_tier, drops is_active,
                  email varchar(120) -> varchar(255), country varchar(2) -> varchar(3)
  orders          amount numeric(10,2) -> double precision,
                  status NOT NULL -> NULL,
                  extra index idx_orders_status in B
  order_items     identical schema, data drift only
DATA
  customers       customer 3 email changed, customer 5 country changed,
                  customer 8 missing in B, customer 9 exists only in B
  orders          order 7 amount 199.99 -> 189.99, order 10 status shipped -> delivered,
                  order 12 missing in B, order 13 only in B
  order_items     items 4 and 17 have a different qty in B
"""
import sys

import psycopg2

import config

DDL_A = """
CREATE TABLE customers (
    customer_id  integer PRIMARY KEY,
    full_name    varchar(100) NOT NULL,
    email        varchar(120) NOT NULL,
    country      varchar(2),
    signup_date  date,
    is_active    boolean DEFAULT true
);
CREATE TABLE orders (
    order_id     integer PRIMARY KEY,
    customer_id  integer NOT NULL REFERENCES customers(customer_id),
    order_date   date NOT NULL,
    status       varchar(20) NOT NULL,
    amount       numeric(10,2) NOT NULL
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE TABLE order_items (
    item_id      integer PRIMARY KEY,
    order_id     integer NOT NULL,
    product_sku  varchar(32) NOT NULL,
    qty          integer NOT NULL,
    unit_price   numeric(10,2) NOT NULL
);
CREATE TABLE products (
    sku       varchar(32) PRIMARY KEY,
    name      varchar(120) NOT NULL,
    category  varchar(40),
    price     numeric(10,2) NOT NULL
);
"""

DDL_B = """
CREATE TABLE customers (
    customer_id   integer PRIMARY KEY,
    full_name     varchar(100) NOT NULL,
    email         varchar(255) NOT NULL,
    country       varchar(3),
    signup_date   date,
    loyalty_tier  varchar(20) DEFAULT 'bronze'
);
CREATE TABLE orders (
    order_id     integer PRIMARY KEY,
    customer_id  integer NOT NULL REFERENCES customers(customer_id),
    order_date   date NOT NULL,
    status       varchar(20),
    amount       double precision NOT NULL
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE TABLE order_items (
    item_id      integer PRIMARY KEY,
    order_id     integer NOT NULL,
    product_sku  varchar(32) NOT NULL,
    qty          integer NOT NULL,
    unit_price   numeric(10,2) NOT NULL
);
CREATE TABLE inventory (
    sku        varchar(32) PRIMARY KEY,
    warehouse  varchar(20) NOT NULL,
    qty        integer NOT NULL
);
"""

CUSTOMERS_A = [
    (1, "Anita Raghavan", "anita@example.com", "IN", "2023-01-14", True),
    (2, "Ben Fischer", "ben.fischer@example.com", "DE", "2023-02-03", True),
    (3, "Chloe Martin", "chloe.martin@example.com", "FR", "2023-02-21", True),
    (4, "Devi Menon", "devi@example.com", "IN", "2023-03-09", True),
    (5, "Eduardo Silva", "eduardo@example.com", "BR", "2023-04-17", False),
    (6, "Fatima Noor", "fatima@example.com", "AE", "2023-05-02", True),
    (7, "Gopal Iyer", "gopal@example.com", "IN", "2023-06-11", True),
    (8, "Hanna Koskinen", "hanna@example.com", "FI", "2023-07-30", True),
]

# B: customer 3 email changed, 5 country changed, 8 absent, 9 added
CUSTOMERS_B = [
    (1, "Anita Raghavan", "anita@example.com", "IN", "2023-01-14", "gold"),
    (2, "Ben Fischer", "ben.fischer@example.com", "DE", "2023-02-03", "silver"),
    (3, "Chloe Martin", "c.martin@newmail.com", "FR", "2023-02-21", "bronze"),
    (4, "Devi Menon", "devi@example.com", "IN", "2023-03-09", "gold"),
    (5, "Eduardo Silva", "eduardo@example.com", "BRA", "2023-04-17", "bronze"),
    (6, "Fatima Noor", "fatima@example.com", "AE", "2023-05-02", "silver"),
    (7, "Gopal Iyer", "gopal@example.com", "IN", "2023-06-11", "bronze"),
    (9, "Ivan Petrov", "ivan@example.com", "RU", "2023-08-19", "bronze"),
]

ORDERS_A = [
    (1, 1, "2024-01-05", "delivered", 249.50),
    (2, 1, "2024-01-22", "delivered", 89.00),
    (3, 2, "2024-02-02", "cancelled", 310.75),
    (4, 3, "2024-02-14", "delivered", 45.20),
    (5, 4, "2024-02-28", "shipped", 1299.00),
    (6, 4, "2024-03-03", "delivered", 76.40),
    (7, 5, "2024-03-19", "delivered", 199.99),
    (8, 6, "2024-04-01", "pending", 512.30),
    (9, 6, "2024-04-15", "delivered", 63.10),
    (10, 7, "2024-05-07", "shipped", 845.00),
    (11, 7, "2024-05-21", "delivered", 129.95),
    (12, 8, "2024-06-02", "pending", 402.00),
]

# B: order 7 amount changed, order 10 status changed, 12 absent, 13 added
ORDERS_B = [
    (1, 1, "2024-01-05", "delivered", 249.50),
    (2, 1, "2024-01-22", "delivered", 89.00),
    (3, 2, "2024-02-02", "cancelled", 310.75),
    (4, 3, "2024-02-14", "delivered", 45.20),
    (5, 4, "2024-02-28", "shipped", 1299.00),
    (6, 4, "2024-03-03", "delivered", 76.40),
    (7, 5, "2024-03-19", "delivered", 189.99),
    (8, 6, "2024-04-01", "pending", 512.30),
    (9, 6, "2024-04-15", "delivered", 63.10),
    (10, 7, "2024-05-07", "delivered", 845.00),
    (11, 7, "2024-05-21", "delivered", 129.95),
    (13, 9, "2024-06-20", "pending", 77.25),
]

SKUS = ["SKU-100", "SKU-101", "SKU-102", "SKU-200", "SKU-201"]


def _order_items(qty_overrides=None):
    qty_overrides = qty_overrides or {}
    rows, item_id = [], 1
    for order_id, *_ in ORDERS_A:
        for n in range(2 if order_id % 2 else 1):
            sku = SKUS[(item_id + n) % len(SKUS)]
            qty = qty_overrides.get(item_id, 1 + (item_id % 4))
            price = round(19.5 + (item_id * 3.25) % 120, 2)
            rows.append((item_id, order_id, sku, qty, price))
            item_id += 1
    return rows


ORDER_ITEMS_A = _order_items()
ORDER_ITEMS_B = [
    r for r in _order_items({4: 9, 17: 12}) if r[1] != 12
]  # order 12 does not exist in B

PRODUCTS_A = [
    ("SKU-100", "Aluminium tumbler", "kitchen", 24.00),
    ("SKU-101", "Cast iron pan", "kitchen", 79.00),
    ("SKU-102", "Linen apron", "kitchen", 32.50),
    ("SKU-200", "Desk lamp", "home office", 115.00),
    ("SKU-201", "Cable tray", "home office", 28.75),
]

INVENTORY_B = [
    ("SKU-100", "BLR-1", 340),
    ("SKU-101", "BLR-1", 52),
    ("SKU-102", "COK-2", 118),
    ("SKU-200", "COK-2", 7),
    ("SKU-201", "BLR-1", 0),
]


def _recreate_database(name):
    conn = psycopg2.connect(config.admin_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()


def _load(dsn, ddl, inserts):
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(ddl)
        for table, columns, rows in inserts:
            placeholders = ", ".join(["%s"] * len(columns))
            cur.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", rows
            )
    conn.commit()
    conn.close()


def main():
    print(f"Server: {config.PG_HOST}:{config.PG_PORT} as {config.PG_USER}")

    for name in (config.DB_A_NAME, config.DB_B_NAME):
        _recreate_database(name)
        print(f"  recreated database {name}")

    _load(
        config.dsn_a(),
        DDL_A,
        [
            (
                "customers",
                ["customer_id", "full_name", "email", "country", "signup_date", "is_active"],
                CUSTOMERS_A,
            ),
            (
                "orders",
                ["order_id", "customer_id", "order_date", "status", "amount"],
                ORDERS_A,
            ),
            (
                "order_items",
                ["item_id", "order_id", "product_sku", "qty", "unit_price"],
                ORDER_ITEMS_A,
            ),
            ("products", ["sku", "name", "category", "price"], PRODUCTS_A),
        ],
    )
    print(f"  loaded {config.DB_A_NAME}: customers, orders, order_items, products")

    _load(
        config.dsn_b(),
        DDL_B,
        [
            (
                "customers",
                ["customer_id", "full_name", "email", "country", "signup_date", "loyalty_tier"],
                CUSTOMERS_B,
            ),
            (
                "orders",
                ["order_id", "customer_id", "order_date", "status", "amount"],
                ORDERS_B,
            ),
            (
                "order_items",
                ["item_id", "order_id", "product_sku", "qty", "unit_price"],
                ORDER_ITEMS_B,
            ),
            ("inventory", ["sku", "warehouse", "qty"], INVENTORY_B),
        ],
    )
    print(f"  loaded {config.DB_B_NAME}: customers, orders, order_items, inventory")
    print("\nSample data ready. Next: streamlit run app.py   (or: python main.py)")


if __name__ == "__main__":
    try:
        main()
    except psycopg2.OperationalError as exc:
        print(f"Cannot reach Postgres: {exc}", file=sys.stderr)
        print("Check PG_HOST / PG_PORT / PG_USER / PG_PASSWORD in your .env", file=sys.stderr)
        sys.exit(1)
