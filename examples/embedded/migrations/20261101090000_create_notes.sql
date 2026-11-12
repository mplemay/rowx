CREATE TABLE notes (
    id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title      text NOT NULL,
    body       text,
    created_at timestamptz NOT NULL DEFAULT now()
);
