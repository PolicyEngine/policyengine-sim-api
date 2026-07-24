# Cloud Run deployment

Infrastructure is bootstrapped with gcloud and application revisions are owned
by the dedicated GitHub Actions workflow.

## One-time bootstrap

Authenticate as an organization principal that can create projects, attach
billing, and administer IAM. Then run:

    SIMULATION_ENTRYPOINT_GCP_PROJECT_ID=policyengine-simulation-entrypoint \
    SIMULATION_ENTRYPOINT_GCP_BILLING_ACCOUNT=BILLING_ACCOUNT_ID \
    bash .github/scripts/bootstrap-cloud-run-simulation-entrypoint.sh

Set SIMULATION_ENTRYPOINT_GCP_FOLDER_ID or SIMULATION_ENTRYPOINT_GCP_ORGANIZATION_ID when project
placement is not inherited automatically.

The script is idempotent. It enables APIs and creates the Artifact Registry
repository, staging/production runtime identities, GitHub deployer identity,
least-privilege IAM bindings, empty Secret Manager secrets, and the dedicated
GitHub workload identity pool/provider.

Use SIMULATION_ENTRYPOINT_BOOTSTRAP_DRY_RUN=1 to print the commands without changing GCP.

## Secrets and GitHub environments

Add one version to each secret created by the bootstrap:

- simulation-entrypoint-old-gateway-client-secret-staging
- simulation-entrypoint-old-gateway-client-secret-production

Configure the printed project/WIF values as repository variables or secrets.
Configure SIMULATION_ENTRYPOINT_RUNTIME_SERVICE_ACCOUNT separately in the staging and
production GitHub environments. Each environment also supplies its own old
gateway URL/client ID and the shared Auth0 issuer/audiences.

## Deploy candidates

The Deploy Simulation Entrypoint to Cloud Run workflow:

1. tests and builds one immutable image;
2. deploys and smoke-tests a tagged no-traffic staging revision;
3. deploys and smoke-tests a tagged no-traffic production revision.

The workflow never changes a service's traffic. After reviewing its smoke
evidence, an authorized operator promotes an exact known-good revision manually:

    SIMULATION_ENTRYPOINT_GCP_PROJECT_ID=policyengine-simulation-entrypoint \
    SIMULATION_ENTRYPOINT_DEPLOYMENT_ENVIRONMENT=staging \
    SIMULATION_ENTRYPOINT_TARGET_REVISION=REVISION_NAME \
    bash .github/scripts/set-cloud-run-simulation-entrypoint-revision.sh

Use `SIMULATION_ENTRYPOINT_DEPLOYMENT_ENVIRONMENT=production` for production. Rollback uses
the same command with the prior known-good revision. The script verifies that
the revision is Ready and belongs to the selected service before assigning it
100 percent. Use `SIMULATION_ENTRYPOINT_TRAFFIC_DRY_RUN=1` to inspect the command without
changing traffic.

After the first successful deployment creates both services, configure the
domain mappings idempotently:

    SIMULATION_ENTRYPOINT_GCP_PROJECT_ID=policyengine-simulation-entrypoint \
    bash .github/scripts/configure-cloud-run-simulation-entrypoint-domains.sh

Inspect the mappings and publish the returned DNS records for
staging.simulation.api.policyengine.org and simulation.api.policyengine.org.
Use SIMULATION_ENTRYPOINT_DOMAINS_DRY_RUN=1 to preview this phase.
