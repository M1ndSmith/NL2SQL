#!/bin/bash
# First boot only. Databases: app (SQL roles), auth (passwords), memory (mem0), checkpoints, audit.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
CREATE ROLE nl2sql_analyst LOGIN PASSWORD 'pw';
CREATE ROLE nl2sql_ops LOGIN PASSWORD 'pw';
CREATE ROLE nl2sql_auth LOGIN PASSWORD 'pw';
CREATE ROLE nl2sql_memory LOGIN PASSWORD 'pw';
CREATE ROLE nl2sql_checkpoint LOGIN PASSWORD 'pw';
CREATE ROLE nl2sql_audit LOGIN PASSWORD 'pw';

CREATE DATABASE app;
CREATE DATABASE auth OWNER nl2sql_auth;
CREATE DATABASE memory OWNER nl2sql_memory;
CREATE DATABASE checkpoints OWNER nl2sql_checkpoint;
CREATE DATABASE audit OWNER nl2sql_audit;

REVOKE ALL ON DATABASE app FROM PUBLIC;
REVOKE ALL ON DATABASE auth FROM PUBLIC;
REVOKE ALL ON DATABASE memory FROM PUBLIC;
REVOKE ALL ON DATABASE checkpoints FROM PUBLIC;
REVOKE ALL ON DATABASE audit FROM PUBLIC;
GRANT CONNECT ON DATABASE app TO nl2sql_analyst, nl2sql_ops;
GRANT CONNECT ON DATABASE auth TO nl2sql_auth;
GRANT CONNECT ON DATABASE memory TO nl2sql_memory;
GRANT CONNECT ON DATABASE checkpoints TO nl2sql_checkpoint;
GRANT CONNECT ON DATABASE audit TO nl2sql_audit;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname app <<'EOSQL'
CREATE TABLE customers (
    id integer PRIMARY KEY,
    name text NOT NULL,
    email text,
    phone text
);
CREATE TABLE products (
    id integer PRIMARY KEY,
    name text NOT NULL,
    price numeric NOT NULL
);
CREATE TABLE orders (
    id integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers (id),
    product_id integer NOT NULL REFERENCES products (id),
    status text NOT NULL,
    amount numeric NOT NULL,
    ordered_at timestamptz NOT NULL
);
CREATE TABLE inventory (
    product_id integer PRIMARY KEY REFERENCES products (id),
    quantity integer NOT NULL
);

INSERT INTO customers (id, name, email, phone) VALUES
    (1, 'Ada', 'ada@example.com', '555-0100'),
    (2, 'Grace', 'grace@example.com', '555-0101');
INSERT INTO products (id, name, price) VALUES
    (1, 'Widget', 10.00),
    (2, 'Gadget', 25.00);
INSERT INTO orders (id, customer_id, product_id, status, amount, ordered_at) VALUES
    (1, 1, 1, 'paid', 10.00, '2026-09-01T00:00:00Z'),
    (2, 1, 2, 'paid', 25.00, '2026-09-15T00:00:00Z'),
    (3, 2, 1, 'open', 10.00, '2026-10-01T00:00:00Z');
INSERT INTO inventory (product_id, quantity) VALUES
    (1, 40),
    (2, 12);

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO nl2sql_analyst, nl2sql_ops;
GRANT SELECT (id, name) ON customers TO nl2sql_analyst;
GRANT SELECT ON orders, products TO nl2sql_analyst;
GRANT SELECT, INSERT, UPDATE, DELETE ON customers, orders, products, inventory TO nl2sql_ops;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname auth <<'EOSQL'
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE users (
    user_id text PRIMARY KEY,
    password_hash text NOT NULL,
    role text NOT NULL
);
INSERT INTO users (user_id, password_hash, role) VALUES
    ('ada', crypt('ada', gen_salt('bf')), 'analyst'),
    ('grace', crypt('grace', gen_salt('bf')), 'ops');
GRANT USAGE ON SCHEMA public TO nl2sql_auth;
GRANT SELECT ON users TO nl2sql_auth;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO nl2sql_auth;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname memory <<'EOSQL'
CREATE EXTENSION IF NOT EXISTS vector;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname checkpoints <<'EOSQL'
GRANT CREATE ON SCHEMA public TO nl2sql_checkpoint;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname audit <<'EOSQL'
CREATE TABLE events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id text NOT NULL,
    role text NOT NULL,
    sql text NOT NULL,
    rowcount integer,
    decision text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON events FROM PUBLIC;
GRANT INSERT ON events TO nl2sql_audit;
GRANT SELECT ON events TO nl2sql_audit;
GRANT USAGE, SELECT ON SEQUENCE events_id_seq TO nl2sql_audit;
EOSQL
