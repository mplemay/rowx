-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
MERGE INTO users u USING (VALUES ($1::int, $2::text)) AS s (id, name) ON u.id = s.id WHEN MATCHED THEN UPDATE SET name = s.name WHEN NOT MATCHED THEN INSERT (id, name) VALUES (s.id, s.name) RETURNING merge_action(), old.name, new.name AS new_name
