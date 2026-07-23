# Cloud Run deployment

Infrastructure is bootstrapped with gcloud and application revisions are owned
by the dedicated GitHub Actions workflow.

## One-time bootstrap

Authenticate as an organization principal that can create projects, attach
billing, and administer IAM. Then run:

    SIMULATION_GCP_PROJECT_ID=policyengine-simulation-api \
    SIMULATION_GCP_BILLING_ACCOUNT=BILLING_ACCOUNT_ID \
    bash .github/scripts/bootstrap-cloud-run-simulation-api.sh

Set SIMULATION_GCP_FOLDER_ID or SIMULATION_GCP_ORGANIZATION_ID when project
placement is not inherited automatically.

The script is idempotent. It enables APIs and creates the Artifact Registry
repository, staging/production runtime identities, GitHub deployer identity,
least-privilege IAM bindings, empty Secret Manager secrets, and the dedicated
GitHub workload identity pool/provider.

Use SIMULATION_BOOTSTRAP_DRY_RUN=1 to print the commands without changing GCP.

## Secrets and GitHub environments

Add one version to each secret created by the bootstrap:

- simulation-api-old-gateway-client-secret-staging
- simulation-api-old-gateway-client-secret-production

Configure the printed project/WIF values as repository variables or secrets.
Configure SIMULATION_RUNTIME_SERVICE_ACCOUNT separately in the staging and
production GitHub environments. Each environment also supplies its own old
gateway URL/client ID and the shared Auth0 issuer/audiences.

## Deploy, promote, and roll back

The Deploy Simulation API to Cloud Run workflow:

1. tests and builds one immutable image;
2. deploys a tagged no-traffic staging revision and promotes it after smoke
   checks;
3. deploys a tagged no-traffic production candidate and smoke-tests it;
4. promotes production only when manually dispatched with
   promote_production=true.

To roll back, manually dispatch the same workflow with rollback_revision set to
the known-good Cloud Run revision name. The workflow restores that revision to
100 percent without rebuilding.

After the first successful deployment creates both services, configure the
domain mappings idempotently:

    SIMULATION_GCP_PROJECT_ID=policyengine-simulation-api \
    bash .github/scripts/configure-cloud-run-simulation-api-domains.sh

Inspect the mappings and publish the returned DNS records for
staging.simulation.api.policyengine.org and simulation.api.policyengine.org.
Use SIMULATION_DOMAINS_DRY_RUN=1 to preview this phase.
