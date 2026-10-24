-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
SELECT x.title, x.rn FROM (SELECT p.title, row_number() OVER () AS rn FROM users u LEFT JOIN posts p ON p.user_id = u.id) x
