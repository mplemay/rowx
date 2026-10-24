-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
SELECT u.name, p.title FROM users u FULL JOIN posts p ON p.user_id = u.id
