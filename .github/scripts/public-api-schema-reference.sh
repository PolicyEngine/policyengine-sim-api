#!/usr/bin/env bash
# Build the Public API's tables with its own v2 migration chain, for the
# comparison with the persistence tests' copies of them (rule
# sim.persistence.public-api-copies-match).
#
# Usage: public-api-schema-reference.sh <Public API checkout>
#
# PUBLIC_API_SCHEMA_DATABASE_URL names the empty database to migrate, as
# postgresql://user:password@host:port/database. The Public API's disposable
# migration mode accepts only the local policyengine_v2_alembic_test
# database, with explicit credentials.
set -euo pipefail

checkout="${1:?usage: public-api-schema-reference.sh <Public API checkout>}"
database_url="${PUBLIC_API_SCHEMA_DATABASE_URL:?PUBLIC_API_SCHEMA_DATABASE_URL must name the database to migrate}"

cd "$checkout"
echo "Public API commit: $(git rev-parse HEAD)"
# The Public API installs its lock on Python 3.12, as its own CI does.
export UV_PYTHON=3.12
uv sync --frozen --extra dev
V2_MIGRATION_DATABASE_URL="postgresql+psycopg://${database_url#*://}" \
  V2_ALEMBIC_DISPOSABLE_TEST=1 \
  uv run --frozen --extra dev alembic -c alembic-v2.ini upgrade head
