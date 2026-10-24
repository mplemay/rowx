-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
WITH RECURSIVE t (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 5) SELECT n FROM t
