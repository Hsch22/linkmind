#!/usr/bin/env bash
# Local PostgreSQL for LinkMind, running from the `linkmind` conda env. The server itself is the launchd service
# com.linkmind.postgres (installed by scripts/dev.sh up); this script sets up the data and schema.
# Data lives in $LINKMIND_HOME/pg (default ~/.linkmind, outside iCloud), listens on 127.0.0.1:5544 only.
#   scripts/pg.sh init      # create the cluster (once; does not start it)
#   scripts/pg.sh createdb  # create the database if missing (server must be running)
#   scripts/pg.sh migrate   # apply core/migrations/*.sql not yet applied
#   scripts/pg.sh status | psql
#   LINKMIND_DB=linkmind_<name> scripts/pg.sh ...   # a tester's database
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONDA_ENV="${LINKMIND_CONDA_ENV:-/opt/miniconda3/envs/linkmind}"
export PATH="$CONDA_ENV/bin:$PATH"
LINKMIND_HOME="${LINKMIND_HOME:-$HOME/.linkmind}"
PGDATA="$LINKMIND_HOME/pg"
PGPORT=5544
DB="${LINKMIND_DB:-linkmind}"  # testers: linkmind_<name>
export PGHOST=127.0.0.1 PGPORT PGOPTIONS="-c client_min_messages=warning"

cmd="${1:-status}"
case "$cmd" in
  init)
    if [[ -f "$PGDATA/PG_VERSION" ]]; then exit 0; fi
    mkdir -p "$PGDATA"
    initdb -D "$PGDATA" -E UTF8 --locale=C --auth=trust -U "$USER" >/dev/null
    {
      echo "listen_addresses = '127.0.0.1'"
      echo "port = $PGPORT"
      echo "unix_socket_directories = '$PGDATA'"
    } >> "$PGDATA/postgresql.conf"
    echo "initialized cluster at $PGDATA"
    ;;
  createdb)
    [[ -n "$(psql -tA -d postgres -c "SELECT 1 FROM pg_database WHERE datname = '$DB'")" ]] \
      || { createdb "$DB" && echo "created database $DB"; }
    ;;
  status)
    pg_isready -q && echo "Postgres accepting connections on 127.0.0.1:$PGPORT" || echo "Postgres not reachable"
    ;;
  migrate)
    psql -q -d "$DB" -v ON_ERROR_STOP=1 -c \
      "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    for f in "$ROOT"/core/migrations/*.sql; do
      v="$(basename "$f" .sql)"
      done_already="$(psql -tA -d "$DB" -c "SELECT 1 FROM schema_migrations WHERE version = '$v'")"
      if [[ -z "$done_already" ]]; then
        { echo "BEGIN;"; cat "$f"; echo "INSERT INTO schema_migrations(version) VALUES ('$v');"; echo "COMMIT;"; } \
          | psql -q -d "$DB" -v ON_ERROR_STOP=1
        echo "applied $v"
      fi
    done
    ;;
  psql)
    shift; exec psql -d "$DB" "$@"
    ;;
  *)
    echo "usage: $0 {init|createdb|migrate|status|psql}"; exit 2
    ;;
esac
