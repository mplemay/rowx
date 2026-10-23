CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;

CREATE TYPE mood AS ENUM ('sad', 'ok', 'happy');
CREATE DOMAIN positive_int AS integer CHECK (VALUE > 0);
CREATE DOMAIN positive_ints AS positive_int[];

CREATE TABLE users (
    id integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    name text NOT NULL,
    email text
);

CREATE TABLE posts (
    id integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    user_id integer NOT NULL REFERENCES users (id),
    title text NOT NULL
);

CREATE TABLE orders (
    id integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    user_id integer NOT NULL REFERENCES users (id),
    amount numeric(12, 2) NOT NULL,
    note text
);

-- one column per design §6 type family
CREATE TABLE all_types (
    id integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    c_bool boolean,
    c_int2 smallint,
    c_int4 integer,
    c_int8 bigint,
    c_oid oid,
    c_float4 real,
    c_float8 double precision,
    c_numeric numeric,
    c_text text,
    c_varchar varchar(20),
    c_bpchar char(5),
    c_name name,
    c_citext public.citext,
    c_char "char",
    c_bytea bytea,
    c_date date,
    c_time time,
    c_timetz timetz,
    c_timestamp timestamp,
    c_timestamptz timestamptz,
    c_interval interval,
    c_uuid uuid,
    c_json json,
    c_jsonb jsonb,
    c_mood mood,
    c_domain positive_int,
    c_domain_array positive_ints,
    c_int4_array integer[],
    c_text_array text[],
    c_int4_matrix integer[][],
    c_mood_array mood[]
);

-- unsupported types, for unsupported-type cases
CREATE TABLE unsupported_types (
    id integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    c_money money,
    c_inet inet,
    c_int4range int4range
);

-- a NOT VALID not-null constraint: attnotnull is set, but existing rows are not guaranteed to comply
CREATE TABLE not_valid (
    id integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    x integer
);
ALTER TABLE not_valid ADD CONSTRAINT x_not_null NOT NULL x NOT VALID;

CREATE VIEW user_posts AS
SELECT u.id AS user_id, u.name, p.id AS post_id, p.title
FROM users u
LEFT JOIN posts p ON p.user_id = u.id;
