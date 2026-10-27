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

The Simulation Entrypoint owns the `simulation` schema in the Postgres database
that holds the Public API's v2 tables. Before the entrypoint candidate deploys
in each environment, the `migrate_simulation_database` job applies the
schema's Alembic chain (`migrations/`) with the migrator role. It is the only
job that receives `SIMULATION_MIGRATION_DATABASE_URL`, a GitHub environment
secret in `beta` and `prod`, passed in that step's `env:` alone. If the
migration fails, the entrypoint is not deployed. Only a deployment from `main`
applies revisions; one dispatched from another branch (the Stage 12 deploy can
be) only requires the database to be at that branch's head, so an unmerged
revision never reaches a shared database.

The deploy service account must be able to list the versions of the runtime
URL's secret (`gcloud secrets versions list`), as for the other runtime
secrets.

The entrypoint itself connects as the runtime role. The deploy step resolves
the Secret Manager secret named by the `SIMULATION_DATABASE_URL_SECRET_NAME`
environment variable to its newest enabled version and mounts that version as
`SIMULATION_DATABASE_URL`. Only the entrypoint's runtime service account may
read it. Modal never receives either URL.

`GET /ready` reports the database's applied revision next to the revision the
image expects, under `simulation_database`. The database does not change the
readiness status, because no old-gateway or Stage 12 route uses it. The
candidate smoke check requires the two revisions to match, which shows that the
runtime secret reached the revision and the migration ran. While a cold
candidate has not reached its database yet, it asks again, up to six requests
in all; the stable and
rollback checks do not, because an older image may run against a newer schema.
The ownership rules and roles are described in
`libs/policyengine-simulation-persistence/README.md`.

## Recording run outcomes

The Simulation Entrypoint records a run's outcome in the `simulation` schema
within a request; nothing runs between requests, so Cloud Run keeps no CPU
allocated outside them. Every deploy mounts sim's own Modal token as
`MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`, whatever the Stage 12 setting: the
deploy step resolves the Secret Manager secrets named by the `beta` and `prod`
environment variables `MODAL_TOKEN_ID_SECRET_NAME` and
`MODAL_TOKEN_SECRET_SECRET_NAME` to their newest enabled versions. The runtime
service account must be able to read both secrets. The Stage 12 comparison
path's Modal calls use the same token until it is removed.

A Modal call reports that it ended on `POST /internal/modal-calls/ended`
(`policyengine_simulation_entry.call_report_route`), with the Modal identity
token of the `report_call_ended` function as a bearer token. The entrypoint
checks the token against Modal's published keys and accepts only that
function, in a v2 app, of the workspace in the `beta`/`prod` environment
variable `MODAL_WORKSPACE_ID` and of the deployment's own Modal environment;
the deploy passes both to the runtime as
`SIMULATION_CALL_REPORT_MODAL_WORKSPACE_ID` and
`SIMULATION_CALL_REPORT_MODAL_ENVIRONMENT`, and stops when the workspace ID is
missing. The service already allows unauthenticated requests through Cloud
Run (`--allow-unauthenticated`): callers prove who they are to the
entrypoint, so Modal needs no Google credentials.

The reports come from the v2 Modal app's `report_call_ended` function, which
the v2 functions start as their last step (`src/modal/call_reports.py` in the
executor). It posts to the entrypoint's main URL: the v2 deploy job reads the
service's stable URL (`status.url`) with
`.github/scripts/simulation-entry-main-url.sh` and passes it to `modal deploy`
as `SIMULATION_ENTRY_URL`. Only the revision that serves traffic receives
these requests; staging and production deployments both promote the revision
they tested, so the main URL reaches it once promotion succeeds.

Household calculations (`POST /v2/household-calculations`, see the README)
start the v2 household functions' calls in the same Modal environment,
`SIMULATION_CALL_REPORT_MODAL_ENVIRONMENT`, with the same token, and read the
v2 version manifest there; they need no other setting. Their outcomes are
recorded like any other call's: on the call's report, or by the safety check.

### Staging household test

After each staging deployment promotes its revision, with the v2 application
deployed, the `staging_household_test` job runs
`authenticated_tests/test_staging_household_calculation.py` against the
staging stable URL: it submits a US household calculation, polls until it
succeeds or fails within ten minutes, and reads the result back. It runs
after promotion because the household function reports the call's end to the
stable URL. A failure fails the deployment run, so production does not
deploy.

The calculation refers to rows the Public API owns in the same database, so
the test first reads the staging Public API's US catalog version for the
deployed PolicyEngine.py version (or its current one, when the Public API
has not published that version yet) and stores a one-person household there
(`POST /v2/households`, which returns the same household every time). The
`beta` environment variable `PUBLIC_API_URL` names the staging Public API's
URL. The test authenticates as the deployed-test client
(`SIMULATION_ENTRYPOINT_TEST_AUTH_*`), which needs `simulation:submit` and
`simulation:read` when permissions are enforced. The other runs of
`authenticated_tests` skip it: only this job sets
`SIMULATION_STAGING_HOUSEHOLD_TEST=1`.

## Safety check

A call whose report never arrives is recorded by the safety check, the Cloud
Run job `<entrypoint service>-safety-check`. Each execution runs one recording
pass (`python -m policyengine_simulation_entry.safety_check`) and exits: it
fails runs left `dispatching` for ten minutes and records what Modal reports
for calls dispatched, or last checked, at least five minutes ago. Cloud
Scheduler starts it every five minutes (`*/5 * * * *`, UTC): a lost report is
recorded within about ten minutes, and each execution is a single short
container start, so the job costs little when every call reports.

`.github/scripts/deploy-cloud-run-simulation-safety-check.sh` points the job
at one entrypoint revision. It copies that revision's image digest, service
account, the secret versions it pins for `SIMULATION_DATABASE_URL`,
`MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`, and its plain telemetry settings,
then creates or updates the schedule. Only two places call it, so the job
only ever runs a revision that serves traffic and a rejected candidate never
records anything:

- the promotion job, after the promoted revision passes every check; if this
  step fails, the previous revision is restored and the job is pointed back
  at it, and if that fails too, the schedule is paused;
- the rollback workflow, after it restores entrypoint traffic; if this step
  fails, the schedule is paused, so the job never keeps running the image
  traffic just moved away from.

A revision deployed before the safety check existed (without
`SIMULATION_CALL_REPORT_MODAL_WORKSPACE_ID`) has no job code, so pointing the
job at it pauses the schedule instead.

Staging deployments promote their tested revision too, so staging has the
safety check as production does.

One-time operator steps, in each environment: enable the Cloud Scheduler
API; create the scheduler's service account, give it `roles/run.invoker`
on the job (or the project), and set its email in the GitHub environment
variable `SIMULATION_SAFETY_CHECK_SCHEDULER_SERVICE_ACCOUNT`; give the deploy
service account permission to deploy Cloud Run jobs and manage Cloud
Scheduler jobs, and `iam.serviceAccounts.actAs` on the scheduler's service
account. The job uses the entrypoint's runtime service account, which can
already read its secrets.

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
clean-up also stops the production deployment. Both environments' tests use
the tagged candidate URL, so the currently serving revision remains unchanged
during qualification.

## Promotion and rollback

Both `beta` and `prod` promote. After an environment's complete suite passes,
the workflow verifies that the exact tested revision belongs to that
environment's service and is ready. It also checks that stable traffic still
points to the revision recorded before deployment. If those checks pass, it
assigns all stable service traffic to the tested revision.

In `beta`, promotion keeps the staging service's stable URL on the revision
that passed the staging tests, so staging callers of that URL, such as the
Public API's staging service, reach the build the deployment tested. The
staging deployment of the **Deploy Stage 12 comparison path** workflow promotes
in the same way. Rule `sim.deploy.staging-promotes-tested-revision` requires
both.

Both workflows can also be started manually, and the `beta` environment does
not restrict which branch a run may come from. A manual run from a branch other
than `main` therefore promotes that branch's build to the staging stable URL,
and every staging caller of that URL reaches the unmerged build until the next
deployment from `main` promotes again. Start a staging deployment from another
branch only when that is intended.

The workflow then checks the stable generated Cloud Run URL. It uses the
runtime revision header to prove that the stable URL reaches the exact revision
that passed candidate tests, and it repeats the deployed authentication checks.
If an immediate post-promotion check fails, the workflow restores the exact
previous revision recorded before deployment and fails visibly.

After a successful promotion and deployment, the workflow deletes the
service's older revisions. It keeps only the promoted revision and the one it
replaced, which `simulation-rollback.yml` can route back to.

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
at the next entrypoint deployment, which both `beta` and `prod` promote to
stable traffic once their tests pass. A `beta` value therefore applies to every
caller of the staging stable URL as well as to the deployment tests. The route
permissions are listed in `README.md`.

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
