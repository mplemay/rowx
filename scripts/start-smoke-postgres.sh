#!/usr/bin/env bash
# Start the release-smoke Postgres. The next workflow step execs into the container named pg.
set -euo pipefail

docker run -d --name pg -p 5432:5432 \
  -e POSTGRES_USER=rowx -e POSTGRES_PASSWORD=rowx -e POSTGRES_DB=rowx \
  --health-cmd "pg_isready -U rowx -d rowx" --health-interval 2s --health-retries 30 \
  postgres:18
until [ "$(docker inspect -f '{{.State.Health.Status}}' pg)" = healthy ]; do
  sleep 1
done
