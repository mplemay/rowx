-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
INSERT INTO users (id, name) VALUES ($1, $2) RETURNING id, name, email
