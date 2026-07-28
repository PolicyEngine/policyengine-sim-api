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

Production may use a persistent custom hostname mapped to its stable Cloud Run
service. The mapping follows the service's active traffic assignment and is
not recreated for each release. When a custom hostname is configured as
protected environment metadata, post-promotion checks verify it in addition
to the generated stable URL.

Domain ownership, mapping creation, DNS publication, managed TLS activation,
and infrastructure administration are one-time operator actions governed by
private runbooks rather than committed bootstrap scripts in this public
repository.
