INSERT INTO users (name, email) VALUES
    ('alice', 'alice@example.com'),
    ('bob', NULL),
    ('carol', NULL);

INSERT INTO posts (user_id, title) SELECT id, 'hello from ' || name FROM users WHERE name = 'alice';

INSERT INTO orders (user_id, amount, note) SELECT id, 10.50, NULL FROM users WHERE name = 'alice';
INSERT INTO orders (user_id, amount, note) SELECT id, 99.99, 'rush' FROM users WHERE name = 'bob';

-- outer-join coverage: rows on both sides without a match, plus NULL keys on the nullable columns
-- (users.email, orders.note). Posts and orders that match no user email, an order note equal to a post title,
-- and users with no posts or orders (carol) or a NULL email (bob, carol)
INSERT INTO posts (user_id, title) SELECT id, 'alice@example.com' FROM users WHERE name = 'alice';
INSERT INTO posts (user_id, title) SELECT id, 'orphan title' FROM users WHERE name = 'bob';
INSERT INTO orders (user_id, amount, note) SELECT id, 0.00, 'alice@example.com' FROM users WHERE name = 'bob';
INSERT INTO orders (user_id, amount, note) SELECT id, 0.00, 'orphan note' FROM users WHERE name = 'bob';

-- all-NULL row, then a fully populated one
INSERT INTO all_types DEFAULT VALUES;
INSERT INTO all_types (
    c_bool, c_int2, c_int4, c_int8, c_oid, c_float4, c_float8, c_numeric, c_text, c_varchar, c_bpchar, c_name,
    c_citext, c_char, c_bytea, c_date, c_time, c_timetz, c_timestamp, c_timestamptz, c_interval, c_uuid, c_json,
    c_jsonb, c_mood, c_domain, c_domain_array, c_int4_array, c_text_array, c_int4_matrix, c_mood_array
) VALUES (
    true, 1, 2, 3, 4, 1.5, 2.5, 12345.6789, 'text', 'varchar', 'bp', 'name',
    'CiText', 'c', '\xdeadbeef', '2026-10-23', '12:34:56', '12:34:56+02', '2026-10-23 12:34:56',
    '2026-10-23 12:34:56+00', '1 day 02:03:04', '00000000-0000-0000-0000-000000000001', '{"a": 1}',
    '{"a": 1}', 'ok', 5, ARRAY[1, 2], ARRAY[1, NULL, 3], ARRAY['a', NULL], ARRAY[[1, 2], [3, 4]], ARRAY['sad', 'happy']::mood[]
);

INSERT INTO unsupported_types DEFAULT VALUES;
