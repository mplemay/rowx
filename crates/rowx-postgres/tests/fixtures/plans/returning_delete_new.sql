-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
DELETE FROM users WHERE id = $1 RETURNING new.name
