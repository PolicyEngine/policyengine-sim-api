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
