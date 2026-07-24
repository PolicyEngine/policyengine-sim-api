# Cloud Run deployment

The Simulation Entry service is deployed to Cloud Run by a dedicated GitHub
Actions workflow. This document describes the deployment model only. Concrete
cloud projects, identities, IAM bindings, domains, and secret resources are
managed through private operator documentation.

## Infrastructure

Initial cloud infrastructure is configured once by an authorized operator. It
is intentionally not provisioned by a script in this repository.

The deployment environment must provide:

- an image registry and Cloud Run services;
- separate non-production and production runtime identities;
- GitHub workload identity federation with narrowly scoped trust;
- protected environment configuration and secret storage; and
- domain and traffic-management configuration.

Credential values belong in the cloud secret store. They must not be committed
to the repository or stored as plain GitHub variables. The deployment workflow
receives only the configuration needed to reference those protected resources.

## Candidate deployment

For each eligible revision, the workflow:

1. runs the normal test suite;
2. builds and publishes an immutable container image;
3. deploys a tagged, no-traffic non-production candidate;
4. runs public and authenticated checks against that candidate;
5. deploys a tagged, no-traffic production candidate; and
6. repeats the qualification checks against the production candidate.

The workflow does not assign production traffic.

## Promotion and rollback

An authorized operator promotes an exact, qualified revision after reviewing
the deployment evidence. The operator verifies that the revision belongs to
the intended service and is ready before changing traffic.

Rollback follows the same process, targeting the previous known-good revision.
Traffic changes, domain setup, and infrastructure administration are governed
by private operational runbooks rather than this public repository.
