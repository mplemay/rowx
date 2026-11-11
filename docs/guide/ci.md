# CI recipe

Run the checker against a throwaway `postgres:18` service that has your schema applied.

```yaml
name: rowx

on: [push, pull_request]

jobs:
  check:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:18
        env:
          POSTGRES_USER: app
          POSTGRES_PASSWORD: app
          POSTGRES_DB: app
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U app -d app"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    env:
      DATABASE_URL: postgresql://app:app@localhost:5432/app
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v7
      - run: uv sync
      # migrate: replace with your migration tool, or use `rowx migrate run` (see Migrations)
      - run: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema.sql
      - run: uv run rowx check src
```

Pin the action versions to commit hashes in your own workflow. Migrate **before** checking: the checker validates
against whatever schema the database has. With SQL file migrations, [`rowx check --scratch`](migrations.md#checking)
builds the database itself.
