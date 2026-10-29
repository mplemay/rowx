-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
WITH a AS MATERIALIZED (SELECT u.id, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id), b AS MATERIALIZED (SELECT id, title FROM a) SELECT id, title FROM b
