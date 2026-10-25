-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
UPDATE users SET name = $2 WHERE id = $1 RETURNING WITH (OLD AS o, NEW AS n) o.name, n.name AS new_name
