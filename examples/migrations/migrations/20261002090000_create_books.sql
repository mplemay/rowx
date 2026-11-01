CREATE TABLE books (
    id        integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    author_id integer NOT NULL REFERENCES authors (id),
    title     text NOT NULL,
    published date
);
