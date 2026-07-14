# PostgreSQL Setup

The bot supports SQLite by default and PostgreSQL for longer overnight runs.
Trading behavior remains paper/demo only; changing the database backend does not
enable live execution.

## Local Postgres

```bash
make postgres-up
```

Default local settings:

```bash
DB_BACKEND=postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=kalshi_predictive_bot
POSTGRES_USER=kalshi
POSTGRES_PASSWORD=kalshi_dev_password
```

You can also set a full URL:

```bash
DATABASE_URL=postgresql+psycopg://kalshi:kalshi_dev_password@localhost:5432/kalshi_predictive_bot
```

## Initialize And Check

```bash
kalshi-bot db-migrate
kalshi-bot db-health
kalshi-bot db-doctor
```

The SQLAlchemy engine uses `pool_pre_ping`, `pool_size=10`,
`max_overflow=20`, `pool_timeout=30`, and `READ COMMITTED` isolation for
PostgreSQL.

## Optional SQLite Copy

```bash
kalshi-bot sqlite-backup
kalshi-bot migrate-sqlite-to-postgres --sqlite-path data/kalshi_phase1.db
kalshi-bot db-health
```

Run this only when no overnight or UI write-heavy job is active.

## Overnight Safety

Set this when you want overnight runs to refuse SQLite:

```bash
REQUIRE_POSTGRES_FOR_OVERNIGHT=true
```

`tonight-check` will block if that flag is set and the active backend is not
PostgreSQL.
