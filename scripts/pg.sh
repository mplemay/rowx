#!/usr/bin/env bash
# Provision the test postgres: `pg.sh up [--ssl]`, `pg.sh down`, `pg.sh dsn`.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state="$root/.rowx-test-dsn"
user=rowx
password=rowx
database=rowx

# state file: line 1 is the container id, line 2 is the DSN
container_id() { sed -n 1p "$state"; }

cmd="${1:-}"
case "$cmd" in
up)
  ssl=0
  [[ "${2:-}" == "--ssl" ]] && ssl=1
  if [[ -f "$state" ]] && docker inspect "$(container_id)" >/dev/null 2>&1; then
    echo "test postgres already running; run '$0 down' first" >&2
    exit 1
  fi
  args=(-d -p 127.0.0.1::5432 -e "POSTGRES_USER=$user" -e "POSTGRES_PASSWORD=$password" -e "POSTGRES_DB=$database")
  pg_args=(-c max_connections=200)
  if ((ssl)); then
    "$root/scripts/gen-certs.sh"
    certs="$root/.rowx-certs"
    args+=(-v "$certs:/certs:ro")
    pg_args+=(-c ssl=on -c ssl_cert_file=/certs/server.crt -c ssl_key_file=/certs/server.key)
  fi
  id="$(docker run "${args[@]}" postgres:18 "${pg_args[@]}")"
  # the container logs to stderr, so keep stdout to the DSN only
  for _ in $(seq 1 60); do
    docker exec "$id" pg_isready -U "$user" -d "$database" -h 127.0.0.1 >/dev/null 2>&1 && break
    sleep 1
  done
  if ! docker exec "$id" pg_isready -U "$user" -d "$database" -h 127.0.0.1 >/dev/null 2>&1; then
    docker rm -f "$id" >/dev/null 2>&1 || true
    echo "postgres did not become ready" >&2
    exit 1
  fi
  port="$(docker port "$id" 5432/tcp | head -n1 | sed 's/.*://')"
  dsn="postgresql://$user:$password@127.0.0.1:$port/$database"
  printf '%s\n%s\n' "$id" "$dsn" >"$state"
  echo "$dsn"
  ;;
down)
  if [[ -f "$state" ]]; then
    docker rm -f -v "$(container_id)" >/dev/null 2>&1 || true
    rm -f "$state"
  fi
  ;;
dsn)
  [[ -f "$state" ]] || { echo "test postgres is not running" >&2; exit 1; }
  sed -n 2p "$state"
  ;;
*)
  echo "usage: $0 up [--ssl] | down | dsn" >&2
  exit 2
  ;;
esac
