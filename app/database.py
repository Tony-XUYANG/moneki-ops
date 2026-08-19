from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
    store_id TEXT PRIMARY KEY,
    store_name TEXT NOT NULL,
    category TEXT NOT NULL,
    district TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    product_category TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0)
);

CREATE TABLE IF NOT EXISTS sales_raw (
    source_line INTEGER PRIMARY KEY,
    order_id TEXT,
    sale_date TEXT,
    store_id TEXT,
    product_id TEXT,
    qty TEXT,
    amount TEXT,
    payment TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales_clean (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_line INTEGER NOT NULL UNIQUE,
    order_id TEXT NOT NULL,
    sale_date TEXT NOT NULL,
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    payment TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales_rejected (
    source_line INTEGER PRIMARY KEY,
    reason_codes TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    raw_rows INTEGER NOT NULL,
    accepted_rows INTEGER NOT NULL,
    rejected_rows INTEGER NOT NULL,
    reason_counts TEXT NOT NULL,
    price_mismatch_rows INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sales_date ON sales_clean(sale_date);
CREATE INDEX IF NOT EXISTS idx_sales_store_date ON sales_clean(store_id, sale_date);
CREATE INDEX IF NOT EXISTS idx_sales_product_date ON sales_clean(product_id, sale_date);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()

