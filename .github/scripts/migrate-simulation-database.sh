#!/usr/bin/env bash
# Bring the simulation schema to the chain's head, or require it to be there.
# Run from libs/policyengine-simulation-persistence, whose locked environment
# (with the migrations extra) provides Alembic. migrations/env.py reads the
# migrator URL from SIMULATION_MIGRATION_DATABASE_URL; this script never
# prints it.
#
# SIMULATION_MIGRATION_MODE:
#   upgrade  apply every pending revision, then require the database at the
#            head (deployments from the default branch);
#   verify   apply nothing and only require the database at this checkout's
#            head, so a deployment dispatched from another branch can never
#            apply that branch's revisions.

set -euo pipefail
set +x

if [[ -z "${SIMULATION_MIGRATION_DATABASE_URL:-}" ]]; then
  printf 'SIMULATION_MIGRATION_DATABASE_URL must name the simulation database\n' >&2
  exit 1
fi

mode="${SIMULATION_MIGRATION_MODE:-}"
case "${mode}" in
  upgrade | verify) ;;
  *)
    printf 'SIMULATION_MIGRATION_MODE must be upgrade or verify, not %s\n' "${mode:-empty}" >&2
    exit 1
    ;;
esac

alembic_config="../../migrations/alembic.ini"

if [[ "${mode}" == "upgrade" ]]; then
  uv run --frozen --extra migrations alembic -c "${alembic_config}" upgrade head
fi
uv run --frozen --extra migrations alembic -c "${alembic_config}" current --check-heads
