-- Iris Flower reporting — schema matching app.py's SQLAlchemy models.
-- Run this once against a fresh database. It's also exactly what
-- db.create_all() would generate on app startup, so in most cases you
-- don't need to run this by hand at all — just start the app once with
-- DATABASE_URL pointed at an empty database and Flask-SQLAlchemy will
-- create these tables for you automatically.

CREATE TABLE IF NOT EXISTS orders (
    id             SERIAL PRIMARY KEY,
    date           VARCHAR(50)  NOT NULL,
    source         VARCHAR(100) DEFAULT '-',
    customer       VARCHAR(100) NOT NULL,
    total_price    INTEGER      NOT NULL DEFAULT 0,
    time           VARCHAR(50)  DEFAULT '-',
    address        TEXT         DEFAULT '-',
    wrapped_by     TEXT         DEFAULT '',   -- 包花员工 / ပန်းစည်းစည်းသူ
    is_paid        BOOLEAN      NOT NULL DEFAULT FALSE,
    payment_date   VARCHAR(50)  DEFAULT ''
);

CREATE TABLE IF NOT EXISTS order_items (
    id          SERIAL PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    item_name   TEXT    DEFAULT 'Flower Bouquet',  -- full bouquet description, no size field
    price       INTEGER DEFAULT 0,
    remarks     TEXT    DEFAULT '-'
);

-- Indexes that matter for how the app actually queries data:
-- filtering/grouping by date (day/month/year views), lookups by source,
-- the price-range filter on total_price, and the item_id -> order_id join.
CREATE INDEX IF NOT EXISTS idx_orders_date         ON orders (date);
CREATE INDEX IF NOT EXISTS idx_orders_source        ON orders (source);
CREATE INDEX IF NOT EXISTS idx_orders_total_price   ON orders (total_price);
CREATE INDEX IF NOT EXISTS idx_orders_is_paid       ON orders (is_paid);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items (order_id);
