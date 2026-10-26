-- server_version: 18.3 (Debian 18.3-1.pgdg13+1)
SELECT t0.id, count(*) FROM users t0 FULL JOIN users t2 ON true WHERE t0.email IS NOT NULL GROUP BY CUBE (t0.id)
