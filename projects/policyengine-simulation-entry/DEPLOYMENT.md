# Simulation deployment

The Simulation Entry service is deployed to Cloud Run as one layer of the
simulation API deployment. This document describes the deployment model only.
Concrete cloud projects, identities, IAM bindings, domains, and secret
resources are managed through private operator documentation.

## Infrastructure

Initial cloud infrastructure is configured once by an authorized operator. It
is intentionally not provisioned by a script in this repository.

The deployment environment must provide:

- an image registry and Cloud Run services;
- separate `beta` and `prod` runtime identities;
- GitHub workload identity federation with narrowly scoped trust;
- protected environment configuration and secret storage; and
- domain and traffic-management configuration.

Credential values belong in the cloud secret store. They must not be committed
to the repository or stored as plain GitHub variables. The deployment workflow
receives only the configuration needed to reference those protected resources.

## Simulation database

The simulation service connects to the Postgres database that holds the
Public API's tables, with its own login role. This repository has no schema
or migration chain of its own: the Public API's migration chain creates and
changes those tables, and the Public API's privilege manifest grants the
role what it may do on them.

The deploy step resolves the Secret Manager secret named by the
`SIMULATION_SERVICE_DATABASE_URL_SECRET_NAME` environment variable to its
newest enabled version and mounts that version as
`SIMULATION_SERVICE_DATABASE_URL` on every deploy, whatever the Stage 12
setting. The deploy service account must be
able to list the secret's versions (`gcloud secrets versions list`), as for
the other runtime secrets, and only the entrypoint's runtime service account
may read it. A missing or invalid name, or a secret with no enabled version,
stops the deploy before `gcloud run deploy` runs.

So before the first deploy that carries this connection, each environment
needs, in order: the Public API's tables the service writes, migrated in that
environment's database; the service's login role, with the grants the Public
API's privilege manifest declares; the role's URL stored in Secret Manager,
readable by the runtime service account; and the `beta` or `prod`
`SIMULATION_SERVICE_DATABASE_URL_SECRET_NAME` variable naming it. Without the
variable the deploy stops at once; without the tables, the role or its grants
the candidate check below fails.

`GET /ready` reports, under `simulation_database`, whether the check of the
database succeeded within three seconds (`reachable`; false when the
database wasn't reached, refused the role's login, or didn't answer in time)
and whether its role holds every privilege the service needs
(`has_required_privileges`; null when the check failed; the list is in
`libs/policyengine-simulation-persistence`). The route is public, so it says
only yes or no. The service's log says why: a `simulation_database_unreachable`
message names the error's type and the database driver's error with its
SQLSTATE code, when the database sent one (`28P01` for a refused password,
`57014` for a statement timeout), and `simulation_database_missing_privileges`
messages name each privilege missing, such as `INSERT on reports` or
`UPDATE (modal_call_id) on report_runs`. A check
that reached the database answers every request for 30 seconds, so repeated
requests don't each take a database connection; a failed check isn't reused,
so the next request checks again. The
database does not change the readiness status, because no old-gateway or
Stage 12 route uses it. The candidate smoke check
(`REQUIRE_SIMULATION_DATABASE_ACCESS=true`) requires the database reached with
every privilege held, which shows that the role's URL reached the revision and
that the role holds its grants, and points to the revision's Cloud Run logs
when it fails. While the check
fails, as it does for a cold candidate that hasn't reached its database yet,
it asks again, up to six requests in all; a database that answers with a
privilege missing fails at once. The stable
and rollback checks don't require it, because an older image may not use the
database.

## Household calculations

`POST /household-calculations` and `GET /household-calculations/{id}` read
the Public API's household, policy and catalog rows and write
`household_jobs` through the simulation database above, and call the v2
workers' household functions on Modal. Every deploy, whatever the Stage 12
setting, gives the entrypoint:

- `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`, the newest enabled versions of
  the Secret Manager secrets the `beta` or `prod` variables
  `MODAL_TOKEN_ID_SECRET_NAME` and `MODAL_TOKEN_SECRET_SECRET_NAME` name,
  mounted once. The Modal client reads the token from those names, so the
  Stage 12 comparison path, in the same process, uses the same mount. A
  missing name stops the deploy.
- `SIMULATION_V2_MODAL_ENVIRONMENT`, the deploy's Modal environment
  (`staging` or `main`), and `SIMULATION_V2_MANIFEST_NAME`,
  `simulation-api-v2-version-manifest`: where the household routes read the
  v2 version manifest and find the household functions. These are the
  household path's own settings, apart from the `STAGE12_V2_MANIFEST_*`
  settings the Stage 12 block sets.

Without the database URL or the Modal environment the routes answer 503
(`not_configured`). They use only the manifest's default worker, so a
release's household functions serve calculations once
`publish_stage12_v2_default` has moved the default to its worker.

### The staging household check

Every staging deployment runs `staging_household_check` after the v2
default has moved (or, in a deployment without Stage 12, after the
authenticated tests): `.github/scripts/simulation-staging-household-check.sh`
calculates the stored staging household that the `beta` variable
`SIMULATION_STAGING_HOUSEHOLD_ID` names, through the candidate's tagged URL,
with the authenticated tests' client credentials, and requires its job to
succeed within ten minutes. `deployment_ready` requires the check to pass in
staging, so a failure stops the production deployment. The household must
be a US household stored in the staging Public API with a default year; each
run adds one `household_jobs` row for it. An empty or malformed variable
fails the check.

## Economy reports and simulations

`POST /reports` and `POST /simulations`, and their reads, use the same
database, Modal token and v2 manifest settings as the household routes, and
the deploy's `V2_SIMULATION_OUTPUT_BUCKET`, the `beta` or `prod` variable of
that name: the bucket that keeps v2 simulation output files for good. A
report reuses only a succeeded simulation run whose file is in it; the
service compares output URIs with it and never reads the bucket, so it needs
no storage permission. A missing or invalid bucket name stops the deploy.
The coordinator and workers get the same bucket from the Modal secret the
Stage 12 secret sync writes.

### The staging economy check

Every staging deployment runs `staging_economy_check` when the household
check runs: `.github/scripts/simulation-staging-economy-check.sh` creates a
small report, current law against current law for `country/wales` in 2026,
through the candidate's tagged URL with the authenticated tests' client
credentials, follows it through `GET /reports/{report_id}`, and requires its
run to succeed within 30 minutes. `deployment_ready` requires the check to
pass in staging, so a failure stops the production deployment. Each run adds
a report and its runs to the staging database. Once a .py version's first
check has succeeded, later checks on that version reuse its simulation runs,
so they check the coordinator's use of stored outputs rather than the
workers', which the v2 deployment's own validation calls.

### The staging preemption check

Every staging deployment runs `staging_preemption_check` once the economy
check has succeeded. `.github/scripts/simulation-staging-preemption-check.sh`
runs twice, from the executor project, whose environment has the Modal
client:

- `household` calculates the staging household that
  `SIMULATION_STAGING_HOUSEHOLD_ID` names, as the household check does;
- `report` creates the economy check's report again (labelled "Staging
  preemption check"), whose simulation runs reuse that check's, so it costs
  no new simulation.

Once the household job or report run is `running`, that is once the
household function or coordinator has claimed it, the check takes the
`modal_call_id` its read answers, asks Modal's call graph which container
runs that call (`src/modal/utils/describe_call_inputs.py`), and stops that
container with `modal container stop`. Modal sends the container SIGINT and
gives the input to another container under the same call ID, as it does
after a pre-emption. The check requires the record to succeed under that
call, completed at least 5 seconds after the stop, and the call graph to
show the call's input finished in another container. A record completed
sooner proves nothing, and that attempt counts as missed: the stopped
container may have written the success before the signal reached it, and
Modal's rerun then finds the record finished and only returns its summary.
A rerun in a new container can't finish within 5 seconds of the stop (both
times are counted in whole seconds). Run again, the household function or
coordinator resumes its claim. The report's simulation runs are reused, so
its coordinator starts no worker, and the check doesn't cover a coordinator
starting its workers again.

A calculation that finishes before its container is stopped, for example on
a warm container, proves nothing, so the check submits a new one, up to
three attempts. Each new attempt starts 90 seconds after the last one ended.
Modal stops a container left idle for 60 seconds by default, and these
functions don't change that. A container that finished the last calculation
unstopped went idle when the record finished, so the wait covers it, and the
next calculation starts in a new container. A container that Modal ran a
stopped call in again goes idle only when that rerun returns, which may be
after the attempt ended, so the wait covers it only when the rerun returns
within about 30 seconds of the attempt's end. A record that fails, or
doesn't finish within its deadline (10 minutes for the household, 15 for the
report, per attempt), fails the check at once. Modal refuses to stop a
container that has ended; when a stop fails, the check doesn't stop it again
or submit another calculation, but follows the record to its end, so a
record left running by a container that ended for another reason, such as
running out of memory, fails the check at its deadline. Every Modal command
may take 60 seconds.

It uses the repository's Modal token (the repository secrets
`MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`), which must be allowed to read
call graphs and to list and stop containers. Production's deploy jobs use
the same token, and `modal container stop` takes a container ID in any
environment, so the script refuses any `MODAL_ENVIRONMENT` but `staging` and
stops only a container that `modal container list --env staging` lists. It
also uses the authenticated tests' client credentials. `deployment_ready`
requires the check to pass in staging, so a failure stops the production
deployment.
Each attempt adds a household job, or a report and its run, to the staging
database.

## Candidate qualification

For each eligible revision, the workflow first deploys `beta`
candidates for the Cloud Run entrypoint, Modal gateway, and versioned Modal
executor. Each deployment job runs its own service unit tests. Only after all
three jobs pass does the workflow publish routing and run integration and
authentication checks through the complete request path. Beta entrypoint
qualification uses Cloud Run's generated tagged revision URL and does not
require a custom domain.

The `prod` environment follows the same sequence automatically, but cannot
begin until every `beta` deployment, routing, integration, authentication,
promotion, and revision clean-up job has succeeded, so a failed staging
clean-up also stops the production deployment. A run from a branch other than
`main` skips `beta`'s promotion and clean-up, and the `prod` environment then
refuses the run, since it accepts only `main`. Both environments' tests use
the tagged candidate URL, so the currently serving revision remains unchanged
during qualification.

## Promotion and rollback

Both `beta` and `prod` promote, `beta` only for a run from `main`. After an
environment's complete suite passes, the workflow verifies that the exact
tested revision belongs to that environment's service and is ready. It also
checks that stable traffic still points to the revision recorded before
deployment. If those checks pass, it assigns all stable service traffic to the
tested revision.

In `beta`, promotion keeps the staging service's stable URL on the revision
that passed the staging tests, so staging callers of that URL, such as the
Public API's staging service, reach the build the deployment tested. The
staging deployment of the **Deploy Stage 12 comparison path** workflow promotes
in the same way. Rule `sim.deploy.staging-promotes-tested-revision` requires
both.

The workflow then checks the stable generated Cloud Run URL. It uses the
runtime revision header to prove that the stable URL reaches the exact revision
that passed candidate tests, and it repeats the deployed authentication checks.
If an immediate post-promotion check fails, the workflow restores the exact
previous revision recorded before deployment and fails visibly.

After a successful promotion and deployment, the workflow deletes the
service's older revisions. It keeps only the promoted revision and the one it
replaced, which `simulation-rollback.yml` can route back to.

Both workflows can also be started manually, and the `beta` environment does
not restrict which branch a run may come from. Both therefore pass
`promote_entrypoint: ${{ github.ref == 'refs/heads/main' }}`. A manual run from
a branch other than `main` deploys and tests its candidate revision, but leaves
the staging service's Cloud Run traffic where it was and deletes no revisions,
so the last revision deployed from `main` stays available for rollback.

Promotion covers only the Cloud Run entrypoint. A branch run still deploys the
branch's Modal services to the staging Modal environment and publishes their
routing there, so callers of the staging stable URL can reach them: the main
deployment workflow deploys the gateway, the executor and the Stage 12 workers,
and the Stage 12 workflow deploys the Stage 12 workers. A later deployment from
`main` replaces the apps it deploys, but routes and versions that it doesn't
deploy can stay.

The check reads the branch's own copy of the workflow: it stops an accidental
branch run from promoting, not a branch that edits it. Only a branch
restriction on the `beta` environment would enforce it, and that would also
refuse every branch run's candidate deployment and tests, since each of the
reusable workflow's deployment jobs runs in that environment.

Deployment concurrency prevents two releases from changing the entrypoint
service simultaneously. Promotion never targets `LATEST`.

This traffic change applies only to the Simulation Entrypoint Cloud Run
service. API v1 revision traffic and its choice between direct Modal access and
the Cloud Run entrypoint remain separately controlled.

## Service URLs and domains

Cloud Run provides a stable service URL and tagged candidate URLs. Beta uses
those generated URLs; it does not require a custom hostname.

Production uses a persistent canonical hostname served by a separately managed
global external HTTPS load balancer with a serverless Cloud Run backend. The
load balancer follows the service's stable traffic assignment and is not
recreated for each release. The deployment workflow receives the canonical
URL as protected environment metadata and verifies it after every production
promotion, in addition to checking the generated stable URL.

Domain ownership, load-balancer creation, DNS publication, managed TLS
activation, and infrastructure administration are one-time operator actions
governed by private runbooks rather than committed bootstrap scripts in this
public repository. Direct Cloud Run domain mapping is not the production edge.

## Caller permissions

The deployment workflow passes each environment's
`SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS` GitHub variable to Cloud Run. An
unset or empty variable deploys `0`, which records callers that lack a route's
permission but lets them through. `1` rejects those callers with 403. Any other
value fails the deployment before Cloud Run changes. A new value takes effect
at the next entrypoint deployment, which `prod`, and `beta` for a run from
`main`, promote to stable traffic once their tests pass. A `beta` value
therefore applies to every caller of the staging stable URL as well as to the
deployment tests. The route permissions are listed in `README.md`.

### Auth0 prerequisites

Complete these steps in an environment's Auth0 tenant before setting its
variable to `1`:

1. On the Auth0 API whose identifier is the entrypoint's
   `SIMULATION_ENTRYPOINT_AUTH_AUDIENCE`, define the permissions
   `simulation:submit`, `simulation:read` and `stage12:operate`. Enable RBAC
   and **Add Permissions in the Access Token**.
2. Grant permissions on that API to every client that calls the entrypoint:
   - the API v1 machine client: `simulation:submit` and `simulation:read`;
   - the deployed test client (`SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_ID`),
     which the authenticated deployment tests and the beta integration suite
     use: `simulation:submit` and `simulation:read`. Do not grant it
     `stage12:operate`, because the enforced deployment check relies on its
     absence;
   - the Stage 12 operator client: `stage12:operate`.
3. API v1 requests one token for its `GATEWAY_AUTH_AUDIENCE` and presents it to
   both the old Modal gateway and this entrypoint. The RBAC settings and grants
   above therefore apply to the same Auth0 API the old gateway uses. Review
   every existing client of that API, and grant permissions to each one that
   calls the entrypoint. The old gateway does not check these permissions, and
   the entrypoint's own old-gateway client (`OLD_GATEWAY_AUTH_CLIENT_ID`) needs
   none of them.

### Enabling enforcement

1. Deploy with the variable unset or `0`, and complete the Auth0 setup.
2. Watch the `prod` service for `simulation_entry_permission_missing` events
   with `enforced=false`. Each one names the missing permission and the
   caller's `client_id`. Only `prod` is certain to receive requests from every
   real caller: the staging stable URL receives requests only from the staging
   callers configured to use it, and tagged candidates only from the
   deployment tests, so `beta` events cannot show every missing grant. Access
   tokens issued before a grant do not carry it, so wait at least one
   access-token lifetime after the last grant with no new events.
3. Set the `beta` variable to `1` and deploy. With enforcement on, the
   authenticated deployment tests also check that the test client's token is
   rejected on `GET /internal/stage12/reports/{evaluation_id}`, which requires
   `stage12:operate`. Those tests skip this check while the variable is `0`.
4. Set the `prod` variable to `1` and deploy. No deployment test sends API v1's
   own token, so step 2 is the only check that API v1 holds its grants.

To roll back, set the variable to `0` and deploy. The regular deployment
rebuilds and tests `beta` before `prod`. For `prod` alone, the **Deploy Stage
12 comparison path** workflow with `deployment_environment: production` (and the
`DEPLOY_STAGE12_PRODUCTION` confirmation) redeploys and promotes the production
entrypoint, and also redeploys the production Stage 12 resources.
