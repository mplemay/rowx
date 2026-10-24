CREATE TABLE authors (
    id integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    name text NOT NULL UNIQUE,
    bio text
);

CREATE TABLE books (
    id integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    author_id integer NOT NULL REFERENCES authors (id),
    title text NOT NULL,
    price numeric(8, 2) NOT NULL,
    published date,
    tags text[] NOT NULL DEFAULT '{}'
);

INSERT INTO authors (name, bio) VALUES
    ('Ursula Le Guin', 'Wrote The Dispossessed.'),
    ('Octavia Butler', NULL);

INSERT INTO books (author_id, title, price, published, tags) VALUES
    (1, 'The Dispossessed', 12.50, '1974-05-01', '{scifi,anarchism}'),
    (1, 'A Wizard of Earthsea', 9.99, '1968-11-01', '{fantasy}'),
    (2, 'Kindred', 11.00, '1979-06-01', '{scifi,history}'),
    (2, 'Parable of the Sower', 13.25, NULL, '{scifi}');
