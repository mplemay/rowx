-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
SELECT u.name, (SELECT max(p.title) FROM posts p WHERE p.user_id = u.id) AS latest FROM users u WHERE u.id IN (SELECT user_id FROM posts)
