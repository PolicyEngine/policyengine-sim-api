# US regional dataset validation before merge

The `PR image smoke` workflow checks more than package installation and the
national dataset. Its executor command now also calls an ephemeral Modal
function that:

1. Resolves `state/ut` through the real executor region and dataset resolvers.
2. Requires a non-default certified source with its revision and SHA-256 pin.
3. Calls the same `ensure_datasets` API as production workers for 2026, using
   a fresh temporary directory. No prepared year or baseline output is reused.
4. Builds and runs a Utah baseline through the executor's simulation builder,
   requesting WIC participation, WIC benefits, state FIPS, and net income.
5. Requires complete boolean participation in prepared and calculated inputs,
   nonempty Utah outputs, and finite net income and WIC benefits.
6. Prints the installed `.py` version, selected source revision and hash, and
   row counts. Calculation errors propagate and fail the PR check.

The country year-preparation API currently prepares the shared ACS file before
the simulation filters Utah. This is not just a small household-fixture test:
the approximately 9.8-GB certified source must be downloaded and loaded. The
function uses 8 CPUs, 64 GiB of memory, one container, and a hard 30-minute
execution timeout. The workflow allows 60 minutes including image construction
and its other checks. Timeouts and missing credentials are failures, not reasons
to skip this validation.

The function runs in an ephemeral Modal app using the same runtime image as
the existing import check. It creates no scheduled process, public endpoint,
artifact bucket, persistent volume, deployment, or manifest. Prepared files
stay in its temporary directory; it does not write to production GCS or SQL.
The existing US national artifact precompute remains unchanged.

The source is the installed `.py` bundle's public US dataset. Existing GitHub
Modal credentials launch the function; existing UK credentials continue to be
used to construct and validate the shared image. No new environment variable,
secret, permission, or provisioning step is introduced.

The acceptance-condition unit tests use small DataFrames. They do not stand
in for the real loading check. The real check is invoked by
`.github/scripts/modal-image-smoke.sh` through `src/modal/smoke_app.py`, not by
the local Docker integration command that excludes `beta_only` calculations.

With the affected `.py` 6.2.2 release, real preparation is expected to fail on
missing ACS WIC participation. Do not catch, skip, or mark that failure as
expected in CI. First publish the narrowly scoped temporary compatibility fix
in PolicyEngine/policyengine.py#566, then update the executor's two `.py` pins
and frozen lockfile to that actual published release and rerun the check.
