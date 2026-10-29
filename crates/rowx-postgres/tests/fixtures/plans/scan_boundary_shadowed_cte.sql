-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
WITH c AS MATERIALIZED (SELECT u.id, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id), d AS MATERIALIZED (WITH c AS MATERIALIZED (SELECT p.user_id AS id, p.title FROM posts p) SELECT id, title FROM c) SELECT o.title, d.title FROM c o JOIN d ON o.id = d.id
