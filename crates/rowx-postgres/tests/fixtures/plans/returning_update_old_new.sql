-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
UPDATE users SET name = $2 WHERE id = $1 RETURNING old.name, new.name AS new_name
