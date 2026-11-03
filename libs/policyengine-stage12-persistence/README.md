# PolicyEngine Stage 12 persistence

SQLAlchemy Core mappings and transactional DML for two sets of tables, both
owned by `policyengine-api`, which keeps the SQLModel definitions, Alembic
revisions and live schema. This package never changes a schema.

- **The Stage 12 comparison tables** (`Stage12PersistenceStore`): temporary
  state of the comparison path, removed with it.
- **The Public API's run tables** (`ReportRunStore`): `report_runs`,
  `simulation_runs`, `report_run_simulation_runs`, the result tables, and
  `report_run_comparisons`, which keeps a compared pair's results that no
  result table holds. The v2 Modal coordinator and workers write them as the
  Modal worker role (`policyengine_v2_modal_worker`), for a call that names a
  report run. The mappings declare only the columns those writes touch. Every
  status write is one `UPDATE` conditional on the run's status; a report
  run's completion inserts its result rows in the same transaction.

## Tests

The report run tests need a disposable Postgres migrated by the Public API's
v2 chain, with its declared privileges applied. They skip unless both URLs
are set:

- `REPORT_RUN_TEST_DATABASE_URL`: logs in as `policyengine_v2_modal_worker`;
- `REPORT_RUN_TEST_SEED_DATABASE_URL`: logs in as a role that may insert the
  rows the simulation service would create.
