-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
SELECT name, email FROM users GROUP BY GROUPING SETS ((name), (email))
