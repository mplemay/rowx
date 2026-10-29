-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
SELECT x.id, x.pid FROM (SELECT u.id, p.id AS pid, row_number() OVER () FROM users u LEFT JOIN posts p ON p.user_id = u.id) x
