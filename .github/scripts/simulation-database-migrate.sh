#!/bin/bash
# Apply the simulation schema's Alembic chain, then require the database to be
# at the chain's head. Run from libs/policyengine-simulation-persistence, whose
# locked environment (with the migrations extra) provides Alembic.
# migrations/env.py reads the migrator URL from SIMULATION_MIGRATION_DATABASE_URL;
# this script never prints it.

set -euo pipefail
set +x

if [[ -z "${SIMULATION_MIGRATION_DATABASE_URL:-}" ]]; then
  printf 'SIMULATION_MIGRATION_DATABASE_URL must name the simulation database\n' >&2
  exit 1
fi

alembic_config="${SIMULATION_ALEMBIC_CONFIG:-../../migrations/alembic.ini}"

uv run --frozen --extra migrations alembic -c "${alembic_config}" upgrade head
uv run --frozen --extra migrations alembic -c "${alembic_config}" current --check-heads
