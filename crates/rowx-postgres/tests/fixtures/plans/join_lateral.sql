-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
SELECT u.name, l.title FROM users u LEFT JOIN LATERAL (SELECT p.title FROM posts p WHERE p.user_id = u.id ORDER BY p.id LIMIT 1) l ON true
