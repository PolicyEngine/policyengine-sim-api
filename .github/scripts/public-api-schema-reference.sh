#!/usr/bin/env bash
# Build the Public API's tables with its own v2 migration chain, and grant the
# table privileges its manifest declares, for the comparison with sim's table
# models (rule sim.persistence.models-match-public-api).
#
# Usage: public-api-schema-reference.sh <Public API checkout>
#
# PUBLIC_API_SCHEMA_DATABASE_URL names the empty database to build, as
# postgresql://user:password@host:port/database. The Public API's disposable
# migration mode accepts only the local policyengine_v2_alembic_test database,
# with explicit credentials, and creates the roles its manifest manages
# without a login. The script prints the Public API commit it built and, on
# GitHub Actions, passes it to the later steps as PUBLIC_API_SCHEMA_COMMIT, so
# a failed comparison names it.
set -euo pipefail

checkout="${1:?usage: public-api-schema-reference.sh <Public API checkout>}"
database_url="${PUBLIC_API_SCHEMA_DATABASE_URL:?PUBLIC_API_SCHEMA_DATABASE_URL must name the database to build}"

cd "${checkout}"
commit="$(git rev-parse HEAD)"
printf 'Public API commit: %s\n' "${commit}"
if [ -n "${GITHUB_ENV:-}" ]; then
  printf 'PUBLIC_API_SCHEMA_COMMIT=%s\n' "${commit}" >> "${GITHUB_ENV}"
fi
# The Public API installs its lock on Python 3.12, as its own CI does.
export UV_PYTHON=3.12
export V2_MIGRATION_DATABASE_URL="postgresql+psycopg://${database_url#*://}"
export V2_ALEMBIC_DISPOSABLE_TEST=1
export DEPLOYMENT_ENVIRONMENT=ci
uv sync --frozen --extra dev
uv run --frozen --extra dev alembic -c alembic-v2.ini upgrade head
uv run --frozen --extra dev python scripts/reconcile_v2_privileges.py apply --create-missing-roles
