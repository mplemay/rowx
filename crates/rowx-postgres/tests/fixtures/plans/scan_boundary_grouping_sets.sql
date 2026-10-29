-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
SELECT x.name, x.email FROM (SELECT name, email, count(*) FROM users GROUP BY GROUPING SETS ((name), (email))) x
