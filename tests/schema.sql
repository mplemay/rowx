CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS file_fdw WITH SCHEMA public;
CREATE SERVER IF NOT EXISTS rowx_files FOREIGN DATA WRAPPER file_fdw;

CREATE TYPE mood AS ENUM ('sad', 'ok', 'happy');
CREATE DOMAIN positive_int AS integer CHECK (VALUE > 0);
CREATE DOMAIN positive_ints AS positive_int[];
-- a user-defined range over a supported subtype decodes like the built-in range of that subtype
CREATE TYPE rowx_intrange AS RANGE (subtype = integer);
CREATE DOMAIN rowx_short_range AS int4range;
-- its subtype has no range codec, so the range is unsupported
CREATE TYPE rowx_floatrange AS RANGE (subtype = double precision);

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
    c_inet inet,
    c_cidr cidr,
    c_macaddr macaddr,
    c_macaddr8 macaddr8,
    c_int4range int4range,
    c_int8range int8range,
    c_numrange numrange,
    c_daterange daterange,
    c_tsrange tsrange,
    c_tstzrange tstzrange,
    c_money money,
    c_bit bit(8),
    c_varbit varbit(16),
    c_json json,
    c_jsonb jsonb,
    c_mood mood,
    c_domain positive_int,
    c_domain_array positive_ints,
    c_int4_array integer[],
    c_text_array text[],
    c_int4_matrix integer[][],
    c_mood_array mood[],
    c_inet_array inet[],
    c_cidr_array cidr[],
    c_macaddr_array macaddr[],
    c_macaddr8_array macaddr8[],
    c_int4range_array int4range[],
    c_numrange_array numrange[],
    c_tstzrange_array tstzrange[],
    c_custom_range rowx_intrange,
    c_custom_range_array rowx_intrange[],
    c_money_array money[],
    c_bit_array bit(8)[],
    c_varbit_array varbit[],
    c_interval_array interval[]
);

-- unsupported types, for unsupported-type cases
CREATE TABLE unsupported_types (
    id integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    c_xml xml,
    c_tsvector tsvector,
    c_int4multirange int4multirange,
    c_floatrange rowx_floatrange
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

-- describe only prepares, so the file is never read
CREATE FOREIGN TABLE foreign_users (
    id integer NOT NULL,
    name text
) SERVER rowx_files OPTIONS (filename '/dev/null', format 'csv');

-- a procedure with an INOUT parameter, so CALL returns a column (sqlx test_call_procedure)
CREATE PROCEDURE forty_two(INOUT answer integer DEFAULT NULL)
LANGUAGE plpgsql AS $$
BEGIN
    answer := 42;
END;
$$;
