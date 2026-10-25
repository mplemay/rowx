-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
SELECT u.name FROM users u WHERE EXISTS (SELECT 1 FROM posts p WHERE p.user_id = u.id)
