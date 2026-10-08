# US state input-preparation prototype

This opt-in prototype does not change deployments, dataset selection, report
requests, Stage 12 activation, or the production artifact manifest. The related
`policyengine.py` prototype defines the typed input and artifact contracts.
The data files are verified derivatives of one certified national source, not
additional certified datasets.

## Execution

```text
Hash-verified native US source
    → one partition function → 50 state extracts + DC extract
    → bounded Modal dispatch → independent state/year input preparation
```

`partition_certified_us_source` loads all six entity tables once, validates
native membership and state assignments, preserves periods and producer
metadata, and verifies each file by reading it back before publishing its path.
`prepare_us_state_year` uses the same year-extraction helper as national
`create_datasets`, including the bundle's SPM selection and legacy input mapping.
Neither function permits an unmanaged source bypass.

`prepare_state_years_parallel` keeps at most ten calls in flight. Each completed
call immediately releases a slot. A failure stops new dispatch and waits for
already-started calls; it does not retry. Workers return typed artifact and
measurement metadata, never entity DataFrames. JSON serialization occurs only
at the Modal boundary.

## Mandatory first checkpoint

The first checkpoint passed on 2026-10-08; see
[the measurements and limitations](us-state-preparation-dry-run-results.md).
The full-source benchmark still requires explicit resource and budget approval.

Only run the small synthetic benchmark before agreeing a full-run budget and
resources. It must demonstrate all of the following:

- An independent process samples RSS every 100 ms and detects a touched 64 MiB
  allocation held for 600 ms. The process high-water reading cross-checks RSS.
- An intentional exception produces a failed report with valid samples.
- The real partition function writes all 51 extracts of a small synthetic
  source, preserving every entity relationship.
- Two real Modal state/year preparation calls execute concurrently.
  After starting their samplers, both publish readiness on the testing volume
  and wait at most 120 seconds for one another. This isolates concurrency
  verification from variation in fresh-container startup. The controlled wait
  is a separately named phase, not reported as preparation time. Readiness reads
  occur at most once per ten seconds per worker to avoid Modal API rate limits.
- Measurement records identify actual package versions, code revision, source
  hash, phase durations, CPU time, input/output bytes, requested resources,
  sample count, and completion or failure.

Container memory counters are separate from process RSS. The report records an
explicit limitation if the worker cannot read a container memory counter; do not
describe RSS as container memory or use it alone to approve full-run resources.
The sampling process flushes JSONL samples continuously and logs progress every
five seconds. Handled exceptions preserve a final report. Abrupt container
termination can leave only partial samples/logs; it is not a completed benchmark.

Generate the fixture using the analysis-package prototype, then run the executor
prototype with that package's source directory on `PYTHONPATH`:

```bash
# In the policyengine.py prototype checkout:
uv run python -c 'from pathlib import Path; from tests.us_partition_fixtures import write_source; write_source(Path("/tmp/us-preparation-fixture.h5"))'

# In projects/policyengine-simulation-executor in the simulation prototype:
PYTHONPATH=/absolute/path/to/policyengine.py/src uv run python \
  scripts/run_state_preparation_benchmark.py \
  --analysis-repo /absolute/path/to/policyengine.py \
  --synthetic-source /tmp/us-preparation-fixture.h5 \
  --output-dir /tmp/us-preparation-evidence
```

The command rejects a source larger than 10 MiB, never downloads ACS, and creates
resources only in `testing`. A random six-character suffix identifies its app,
functions, and volume. Images contain pinned dependencies and prototype code,
not certified source downloads. Containers are single-use, with no retries and
hard execution/memory limits. No new GitHub or production runtime variable is
required. No new secrets or service accounts are created.

The run ends its temporary app automatically. Download and verify the raw
evidence before deleting its uniquely named volume. Do not delete staging or
production resources.

## Remaining work after approval

The full benchmark has not run. It will measure one certified ACS partition,
California/Utah preparation for 2025–2027, then the remaining state/year pairs,
and compare all 153 results with national preparation followed by filtering.
Production cache selection, shared deployment prerequisites, and API v1 package
updates must wait for the second checkpoint. The current prototype is not a
production integration and must not replace the national-only precompute job.

## Approved full-source pilot

On 2026-10-08, the user approved the full ACS partition and six California/Utah
preparations within an initial $20 spending budget. This approval does not
include the remaining 147 preparations or national reference builds.

The attempted pilot passed partitioning but stopped on missing WIC inputs in
both 2025 preparations. See [the partial measurements and diagnosis](us-acs-preparation-pilot-results.md).
The four 2026–2027 jobs were not submitted; no successful preparation forecast
is available yet.

```bash
PYTHONPATH=/absolute/path/to/policyengine.py/src uv run python \
  scripts/run_acs_preparation_pilot.py \
  --analysis-repo /absolute/path/to/policyengine.py \
  --output-dir /tmp/us-acs-pilot-evidence \
  --budget-usd 20
```

This separate command resolves `populace_us_2024_acs_local` by name from the
installed bundle, downloads and verifies it once, and partitions it in one
eight-core/128-GiB function. It prepares CA/UT for 2025–2027 using eight-core/
64-GiB functions with at most two concurrent calls. Each function has a one-hour
timeout, matching requested and hard-limited memory, an explicit soft CPU limit,
single-use containers, and no configured retries. It does not use the synthetic
test's readiness synchronization or artificial holds.

Before partitioning, the command reserves the requested-resource estimate for
all seven full timeouts plus a five-minute startup allowance per call. It checks
the estimate again against the budget before launching any preparation. This is
not a provider-enforced billing ceiling; replacement containers, actual CPU
usage, image builds, storage, and startup variation can affect billing. Rates
are tracked in `precompute_benchmark.pilot` and were checked against
[Modal pricing](https://modal.com/pricing) on 2026-10-08.

The command creates only a uniquely named testing application and volume, with
the same six-character suffix on its functions. It adds no secrets, runtime
configuration variables, public endpoints, schedules, or production wiring.
The application ends after the pilot. JSON/JSONL evidence is downloaded even on
failure; the volume retains the verified source and derivatives for later
approved work to avoid another download/partition.
