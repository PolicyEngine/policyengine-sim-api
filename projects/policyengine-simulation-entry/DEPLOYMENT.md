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

## Candidate deployment

For each eligible revision, the workflow first deploys `beta`
candidates for the Cloud Run entrypoint, Modal gateway, and versioned Modal
executor. Each deployment job runs its own service unit tests. Only after all
three jobs pass does the workflow publish routing and run integration and
authentication checks through the complete request path.

The `prod` environment follows the same sequence automatically, but cannot
begin until every `beta` deployment, routing, integration, and authentication
job has succeeded.

The workflow does not assign production traffic.

## Promotion and rollback

An authorized operator promotes an exact, qualified revision after reviewing
the deployment evidence. The operator verifies that the revision belongs to
the intended service and is ready before changing traffic.

Rollback follows the same process, targeting the previous known-good revision.
Traffic changes, domain setup, and infrastructure administration are governed
by private operational runbooks rather than this public repository.
