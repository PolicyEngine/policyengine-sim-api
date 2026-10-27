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
these requests. `beta` does not promote its candidate yet, so in staging the
main URL reaches an older revision until it does (plan item 1.39), and
staging calls are recorded by the safety check only once it exists there.

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
  at it;
- the rollback workflow, after it restores entrypoint traffic. A restored
  revision without the simulation database predates the safety check, so the
  schedule is paused.

`beta` does not promote its candidate yet, so staging has no safety check
until it does (plan item 1.39).

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
begin until every `beta` deployment, routing, integration, and authentication
job has succeeded. Production tests also use the tagged candidate URL, so the
currently serving revision remains unchanged during qualification.

## Promotion and rollback

After the complete production suite passes, the workflow verifies that the
exact tested revision belongs to the production service and is ready. It also
checks that stable traffic still points to the revision recorded before
deployment. If those guards pass, it assigns all stable service traffic to the
tested production revision.

The workflow then checks the stable generated Cloud Run URL. It uses the
runtime revision header to prove that the stable URL reaches the exact revision
that passed candidate tests, and it repeats the deployed authentication checks.
If an immediate post-promotion check fails, the workflow restores the exact
previous revision recorded before deployment and fails visibly.

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
at the next entrypoint deployment. In `prod` that deployment is promoted to
stable traffic. `beta` never promotes its candidate revision, so a `beta` value
applies only to the tagged candidate that the deployment tests call. The route
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
   caller's `client_id`. Only `prod` serves real callers such as API v1: a
   `beta` candidate receives only the deployment tests' own requests, so `beta`
   events cannot show a missing API v1 grant. Access tokens issued before a
   grant do not carry it, so wait at least one access-token lifetime after the
   last grant with no new events.
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
