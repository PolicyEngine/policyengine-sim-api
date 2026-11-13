# PolicyEngine Stage 12 persistence

SQLAlchemy Core mappings and transactional DML for two sets of tables, both
owned by `policyengine-api`, which keeps the SQLModel definitions, Alembic
revisions and live schema. This package never changes a schema.

- **The Stage 12 comparison tables** (`Stage12PersistenceStore`): temporary
  state of the comparison path, removed with it.
- **The Public API's run tables** (`ReportRunStore`): `report_runs`, which
  names its baseline and reform simulation runs and holds the cliff impact,
  SPM receipts and data version, `simulation_runs`, the result tables, the
  two labour supply tables included, and `household_jobs`. As the Modal
  worker role (`policyengine_v2_modal_worker`), the v2 Modal coordinator
  claims and records the report run its call names, each v2 worker claims
  and records the one simulation run its call names, the v2 household
  functions record the household job their call names, and the scheduled
  stuck-run sweep fails the runs and household jobs nothing will finish.
  The coordinator also reads a simulation run's status, year, versions,
  dataset, region, output file, recorded call and failure, so a report run
  can use a run that already succeeded instead of calculating it again, and
  a restarted coordinator can release a run its earlier worker holds. The
  mappings declare only the columns those statements read or write, and
  each `UPDATE` sets only columns the role may update. Every status write is
  one `UPDATE` conditional on the row's status and, for a call's claim,
  completion or failure, on its recorded Modal call: a call claims a
  pending or running row that records no call or its own, and completes or
  fails it only while it holds it running. A report run's coordinator also
  fails its unfinished simulation runs, whatever call they record, when the
  report run fails, and releases one its pre-empted attempt's worker holds
  back to pending with no call (`release_simulation_run`). A report run's
  completion sets its own result columns and inserts its result rows in the
  same transaction.
- **Budget windows** (`BudgetWindowStore`): `budget_window_runs`, one row per
  calculation of a budget-window report, whose years are report runs naming
  it and their year. The simulation service creates every year pending and
  starts the first ones; each year's coordinator, when its year ends,
  advances the window. An advance locks the window run's row, then fails the
  window and its waiting years when a year failed, marks it succeeded when
  every year succeeded, or starts the earliest waiting years until
  `BUDGET_WINDOW_MAX_RUNNING_YEARS` (five) are under way, recording each
  call while the year is still pending with no call. A year's coordinator
  call marks its window running. The simulation service's restart check
  advances a window that stopped with years still waiting, as the service
  starts a new one; its starter may fail a year with a message of its own
  (`YearStartRefused`), and its store bounds each statement
  (`statement_timeout_milliseconds`). The stuck-run sweep leaves a waiting
  year to its window, fails the window of a year it fails, and settles a
  window no year advances any more. The Modal worker role and the
  simulation service's role may both advance a window.
- **Household report runs** (`HouseholdReportRunStore`, a `ReportRunStore`):
  one household function call claims a household report run and each of
  its simulation runs, the baseline's and any reform's, records each run's
  household result or failure document in `simulation_runs.household_result`,
  and then marks the report run succeeded or failed. It reads first which
  simulation runs the report run names, to check them against its payload
  and to fail them if it stops.

## Tests

The report run and budget-window tests need a disposable Postgres migrated
by the Public API's v2 chain, with its declared privileges applied. They skip
unless both URLs are set:

- `REPORT_RUN_TEST_DATABASE_URL`: logs in as `policyengine_v2_modal_worker`;
- `REPORT_RUN_TEST_SEED_DATABASE_URL`: logs in as a role that may insert the
  rows the simulation service would create.
