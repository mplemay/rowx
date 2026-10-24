-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
SELECT upper(name), count(*) OVER (), coalesce(email, ''), 1 FROM users
