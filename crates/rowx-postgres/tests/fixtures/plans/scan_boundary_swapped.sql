-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
WITH c AS MATERIALIZED (SELECT p.title AS name, u.name AS title FROM users u LEFT JOIN posts p ON p.user_id = u.id) SELECT name, title FROM c
