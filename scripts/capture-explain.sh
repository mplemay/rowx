#!/usr/bin/env bash
# Capture EXPLAIN fixtures: `capture-explain.sh` rewrites them, `capture-explain.sh --check` regenerates into a temp
# directory and diffs against the committed ones. Uses the container from `pg.sh up` (or $ROWX_PG_CONTAINER).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixtures="$root/crates/rowx-pg/tests/fixtures/plans"
state="$root/.rowx-test-dsn"
scratch_db=rowx_plans

container="${ROWX_PG_CONTAINER:-}"
if [[ -z "$container" && -f "$state" ]]; then
  container="$(sed -n 1p "$state")"
fi
[[ -n "$container" ]] || {
  echo "no test postgres; run 'just db-up' or set ROWX_PG_CONTAINER" >&2
  exit 1
}

mode="write"
case "${1:-}" in
  "") ;;
  --check) mode=check ;;
  *)
    echo "usage: $0 [--check]" >&2
    exit 2
    ;;
esac

psql_admin() { docker exec -i "$container" psql -U rowx -d rowx -X -q -v ON_ERROR_STOP=1 "$@"; }
psql_scratch() { docker exec -i "$container" psql -U rowx -d "$scratch_db" -X -q -v ON_ERROR_STOP=1 "$@"; }

out="$fixtures"
if [[ "$mode" == check ]]; then
  out="$(mktemp -d)"
fi
trap 'psql_admin -c "DROP DATABASE IF EXISTS $scratch_db WITH (FORCE)" >/dev/null 2>&1 || true; [[ "$mode" == check ]] && rm -rf "$out"' EXIT

# a fresh database keeps every plan unqualified and independent of what else lives in the test database
psql_admin -c "DROP DATABASE IF EXISTS $scratch_db WITH (FORCE)" -c "CREATE DATABASE $scratch_db" >/dev/null
psql_scratch >/dev/null <<'SQL'
CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL, email text);
CREATE TABLE posts (id int PRIMARY KEY, user_id int NOT NULL REFERENCES users (id), title text NOT NULL);
CREATE TABLE nv (id int PRIMARY KEY, x int);
ALTER TABLE nv ADD CONSTRAINT nv_x_nn NOT NULL x NOT VALID;
SQL

version="$(psql_scratch -Atc "SHOW server_version")"
mkdir -p "$out"
rm -f "$out"/*.sql "$out"/*.json

# queries: `-- name: <fixture>` starts one, the body runs to the next marker
queries() {
  cat <<'SQL'
-- name: join_left_where_param
SELECT u.name, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE u.id = $1
-- name: join_right
SELECT u.name, p.title FROM posts p RIGHT JOIN users u ON p.user_id = u.id
-- name: join_left_order_by
SELECT u.name, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id ORDER BY p.title
-- name: join_left_trivial_subquery
SELECT x.title FROM (SELECT p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id) x
-- name: join_left_reduced_to_inner
SELECT u.name, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id WHERE p.title IS NOT NULL
-- name: join_full
SELECT u.name, p.title FROM users u FULL JOIN posts p ON p.user_id = u.id
-- name: join_duplicate_columns
SELECT p.title, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id
-- name: join_semi
SELECT u.name FROM users u WHERE EXISTS (SELECT 1 FROM posts p WHERE p.user_id = u.id)
-- name: join_anti
SELECT u.name FROM users u WHERE NOT EXISTS (SELECT 1 FROM posts p WHERE p.user_id = u.id)
-- name: join_nested_subquery
SELECT u.name, (SELECT max(p.title) FROM posts p WHERE p.user_id = u.id) AS latest FROM users u WHERE u.id IN (SELECT user_id FROM posts)
-- name: join_lateral
SELECT u.name, l.title FROM users u LEFT JOIN LATERAL (SELECT p.title FROM posts p WHERE p.user_id = u.id ORDER BY p.id LIMIT 1) l ON true
-- name: setop_union
SELECT name FROM users UNION SELECT title FROM posts
-- name: cte_recursive
WITH RECURSIVE t (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 5) SELECT n FROM t
-- name: cte_materialized
WITH c AS MATERIALIZED (SELECT u.name, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id) SELECT title FROM c
-- name: window_subquery
SELECT x.title, x.rn FROM (SELECT p.title, row_number() OVER () AS rn FROM users u LEFT JOIN posts p ON p.user_id = u.id) x
-- name: grouping_rollup
SELECT name FROM users GROUP BY ROLLUP (name)
-- name: grouping_sets
SELECT name, email FROM users GROUP BY GROUPING SETS ((name), (email))
-- name: returning_insert_old
INSERT INTO users (id, name) VALUES ($1, $2) ON CONFLICT (id) DO UPDATE SET name = excluded.name RETURNING old.name, new.name AS new_name
-- name: returning_update_old_new
UPDATE users SET name = $2 WHERE id = $1 RETURNING old.name, new.name AS new_name
-- name: returning_delete_new
DELETE FROM users WHERE id = $1 RETURNING new.name
-- name: returning_alias
UPDATE users SET name = $2 WHERE id = $1 RETURNING WITH (OLD AS o, NEW AS n) o.name, n.name AS new_name
-- name: merge_returning
MERGE INTO users u USING (VALUES ($1::int, $2::text)) AS s (id, name) ON u.id = s.id WHEN MATCHED THEN UPDATE SET name = s.name WHEN NOT MATCHED THEN INSERT (id, name) VALUES (s.id, s.name) RETURNING merge_action(), old.name, new.name AS new_name
-- name: returning_plain
INSERT INTO users (id, name) VALUES ($1, $2) RETURNING id, name, email
-- name: not_valid_constraint
SELECT id, x FROM nv
-- name: computed
SELECT upper(name), count(*) OVER (), coalesce(email, ''), 1 FROM users
SQL
}

emit() {
  local name="$1" sql="$2"
  {
    echo "-- server_version: $version"
    printf '%s\n' "$sql"
  } >"$out/$name.sql"
  # same layout as the pretty-format-json hook (sorted keys, 2-space indent), so committing doesn't rewrite them
  printf '%s\n' "EXPLAIN (GENERIC_PLAN, VERBOSE, COSTS OFF, FORMAT JSON) $sql" \
    | psql_scratch -At \
    | python3 -I -c 'import json, sys; print(json.dumps(json.load(sys.stdin), indent=2, sort_keys=True, ensure_ascii=False))' \
      >"$out/$name.json"
}

name=
sql=
while IFS= read -r line; do
  if [[ "$line" == "-- name: "* ]]; then
    [[ -z "$name" ]] || emit "$name" "$sql"
    name="${line#-- name: }"
    sql=
  else
    sql="${sql:+$sql$'\n'}$line"
  fi
done < <(queries)
[[ -z "$name" ]] || emit "$name" "$sql"

if [[ "$mode" == check ]]; then
  diff -r "$fixtures" "$out"
  echo "fixtures up to date ($(find "$out" -name '*.json' | wc -l | tr -d ' ') plans, postgres $version)"
else
  echo "wrote $(find "$out" -name '*.json' | wc -l | tr -d ' ') plans, postgres $version"
fi
