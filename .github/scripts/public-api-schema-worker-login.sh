#!/usr/bin/env bash
# Give the Public API's Modal worker role a login on the disposable database
# the Public API schema job built, so the Stage 12 library's tests connect as
# that role (rule sim.persistence.models-match-public-api).
#
# Usage: public-api-schema-worker-login.sh
#
# PUBLIC_API_SCHEMA_DATABASE_URL names that database, as the superuser's
# postgresql:// URL; the Public API's privilege manifest creates its roles
# without a login. MODAL_WORKER_TEST_PASSWORD is the password the tests' URL
# gives, which exists only in the job's database.
set -euo pipefail

database_url="${PUBLIC_API_SCHEMA_DATABASE_URL:?PUBLIC_API_SCHEMA_DATABASE_URL must name the database the job built}"
password="${MODAL_WORKER_TEST_PASSWORD:?MODAL_WORKER_TEST_PASSWORD must give the password the Modal worker role logs in with}"

# psql quotes the password variable as a literal (:'password'), so it never
# appears in the statement itself.
psql "${database_url}" --no-psqlrc --set=ON_ERROR_STOP=1 "--set=password=${password}" <<'SQL'
ALTER ROLE policyengine_v2_modal_worker LOGIN PASSWORD :'password';
SQL
