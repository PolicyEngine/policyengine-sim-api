# UK private-data Hugging Face credential

Simulation deployment uses the selected-repository GitHub organization secret
`PE_UK_PRIVATE_HF_READ_TOKEN`. The token must be fine-grained, named
`pe-uk-private-hf-read-token`, and have read-only access to:

- the model repository `policyengine/policyengine-uk-data-private`;
- the dataset repository `policyengine/populace-uk-private`.

The simulation runtime does not use `policyengine/populace-uk-staging`.
Microcosm build or publication access to that repository belongs in a separate
credential.

## Deployment path

The deployment workflow copies the GitHub value into the Modal secret
`pe-uk-private-hf-read-token` in the selected Modal environment. That Modal
secret contains the `HUGGING_FACE_TOKEN` compatibility key required by
PolicyEngine Core. No other mounted Modal secret may define that key.

Immediately after synchronization, `src/modal/hf_access_smoke.py`:

1. derives the complete UK Hugging Face artifact list from the installed
   policyengine.py bundle;
2. verifies the expected token display name and fine-grained role;
3. rejects any write permission;
4. requests metadata for every pinned artifact from inside Modal;
5. prints only the account name, token name, a short SHA-256 fingerprint, and
   artifact metadata.

This check does not rely on the Modal image cache. Both the existing versioned
workers and the Stage 12 workers mount the same purpose-specific secret.

The simulation-entry Cloud Run service does not receive the credential. It
submits work to Modal but does not access Hugging Face.

## Retiring previous resources

The simulation code no longer mounts `policyengine-data-credentials`; the
resource currently contains only the old `HUGGING_FACE_TOKEN` value. Do not
delete it until all active versioned worker deployments have been audited and
its Modal last-used timestamp remains unchanged through the rollback period.

The Modal secret `huggingface-token` also has an unrelated consumer in the CRFB
long-run publishing tool, which requires write access to US data. Migrate that
tool to its own purpose-specific write credential before deleting the old
Modal secret.

After production validation, remove the repository secret `HF_TOKEN` from
`policyengine-sim-api`. Secrets with the same name in other repositories are
independent and are outside this migration.
