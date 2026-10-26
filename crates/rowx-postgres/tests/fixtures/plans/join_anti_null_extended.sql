-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
SELECT t0.name FROM users t0 RIGHT JOIN users t1 ON t1.email = t0.email WHERE t0.email IS NULL
