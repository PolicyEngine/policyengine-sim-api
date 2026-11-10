# PolicyEngine Stage 12 persistence

SQLAlchemy Core mappings and transactional DML for two sets of tables, both
owned by `policyengine-api`, which keeps the SQLModel definitions, Alembic
revisions and live schema. This package never changes a schema.

- **The Stage 12 comparison tables** (`Stage12PersistenceStore`): temporary
  state of the comparison path, removed with it.
- **The Public API's run tables** (`ReportRunStore`): `report_runs`, which
  names its baseline and reform simulation runs and holds the cliff impact,
  SPM receipts and data version, `simulation_runs`, and the result tables,
  the two labour supply tables included. The v2 Modal coordinator and workers
  write them as the Modal worker role (`policyengine_v2_modal_worker`), for a
  call that names a report run, and the scheduled stuck-run sweep fails the
  runs nothing will finish, as the same role. The coordinator also reads a
  simulation run's status, year, versions and output file, so a report run
  can use a run that already succeeded instead of calculating it again. The
  mappings declare only the columns those statements read or write, and
  each `UPDATE` sets only
  columns the role may update. Every status write is one `UPDATE`
  conditional on the run's status; a report run's completion sets its own
  result columns and inserts its result rows in the same transaction.

## Tests

The report run tests need a disposable Postgres migrated by the Public API's
v2 chain, with its declared privileges applied. They skip unless both URLs
are set:

- `REPORT_RUN_TEST_DATABASE_URL`: logs in as `policyengine_v2_modal_worker`;
- `REPORT_RUN_TEST_SEED_DATABASE_URL`: logs in as a role that may insert the
  rows the simulation service would create.
