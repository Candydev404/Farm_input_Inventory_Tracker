PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    full_name     TEXT    NOT NULL,
    email         TEXT,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'storekeeper' CHECK (role IN ('admin','storekeeper')),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    last_login    TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS suppliers (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    phone   TEXT,
    address TEXT
);

CREATE TABLE IF NOT EXISTS inventory_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code     TEXT    NOT NULL UNIQUE,
    name          TEXT    NOT NULL,
    category_id   INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    description   TEXT,
    quantity      REAL    NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    unit          TEXT    NOT NULL,
    min_threshold REAL    NOT NULL DEFAULT 0 CHECK (min_threshold >= 0),
    unit_price    REAL    NOT NULL DEFAULT 0 CHECK (unit_price >= 0),
    supplier_id   INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    is_returnable INTEGER NOT NULL DEFAULT 0 CHECK (is_returnable IN (0,1)),
    is_sample     INTEGER NOT NULL DEFAULT 0 CHECK (is_sample IN (0,1)),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_items_category ON inventory_items(category_id);
CREATE INDEX IF NOT EXISTS idx_items_name ON inventory_items(name);

CREATE TABLE IF NOT EXISTS stock_transactions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    reference        TEXT    NOT NULL UNIQUE,
    item_id          INTEGER NOT NULL REFERENCES inventory_items(id) ON DELETE RESTRICT,
    txn_type         TEXT    NOT NULL CHECK (txn_type IN ('IN','OUT','LOAN','RETURN')),
    quantity         REAL    NOT NULL CHECK (quantity > 0),
    balance_after    REAL    NOT NULL CHECK (balance_after >= 0),
    unit_price       REAL    NOT NULL DEFAULT 0 CHECK (unit_price >= 0),
    txn_date         TEXT    NOT NULL,
    responsible      TEXT    NOT NULL,
    counterparty     TEXT,
    notes            TEXT,
    created_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    is_sample        INTEGER NOT NULL DEFAULT 0 CHECK (is_sample IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_txn_item ON stock_transactions(item_id);
CREATE INDEX IF NOT EXISTS idx_txn_date ON stock_transactions(txn_date);

CREATE TABLE IF NOT EXISTS system_notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER REFERENCES inventory_items(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('LOW','OUT','INFO')),
    message    TEXT NOT NULL,
    is_read    INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action     TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
