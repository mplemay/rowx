CREATE TABLE products (
    id integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    name text NOT NULL,
    category text NOT NULL,
    vendor text,
    price numeric(8, 2) NOT NULL,
    stock integer NOT NULL,
    -- never selected by the example, and never in its sort allowlist
    internal_cost numeric(8, 2) NOT NULL
);

INSERT INTO products (name, category, vendor, price, stock, internal_cost) VALUES
    ('Anvil', 'hardware', 'Acme', 49.00, 5, 30.00),
    ('Birdseed', 'garden', NULL, 4.50, 120, 4.00),
    ('Catapult', 'hardware', 'Acme', 129.99, 2, 20.00),
    ('Dynamite', 'hardware', 'Roadrunner', 19.95, 40, 10.00),
    ('Earmuffs', 'apparel', 'Acme', 12.00, 15, 11.00);
