# policyengine-simulation-executor

PolicyEngine Simulation API service.

## Modal image dependencies

The executor images (`src/modal/app.py` and `src/modal/v2_app.py`) install
their runtime packages straight from this project's `uv.lock` via
`uv_sync(frozen=True, --only-group modal-simulation-image)`. Image
packages therefore match the versions the test environment runs against
and can only change through a relock — never through a fresh resolution
at image-build time (issue #602 is what happens otherwise).
Both the executor project and `modal-simulation-image` declare
`policyengine[models]` at an exact wrapper version. That extra declares the
wrapper's exact core, country-model, and SPM calculator dependencies, and
`uv.lock` records the versions that uv resolved. Neither dependency list
duplicates those component versions.

After `uv_sync`, `policyengine bundle install --no-packages` downloads and
verifies the certified datasets and writes the bundle receipt. It does not run
pip or alter the locked Python environment. The v1 worker, Stage 12 workers,
precompute, and import-smoke paths share this package and data arrangement.
The gateway lives in its own project and installs from its own lock; see its
README.

To change a PolicyEngine release, update the `policyengine[models]` requirement
in the project dependencies and `modal-simulation-image`, then run `uv lock`.
The automated updater changes only those wrapper requirements; it reads the
wrapper's release manifest to verify and report the selected component and data
versions. Other image dependencies belong in `modal-simulation-image`. PRs
touching image inputs run an in-image import smoke (`src/modal/smoke_app.py` via
`.github/workflows/pr-image-smoke.yml`). Note that any change to the
group or lock invalidates the image layer cache, including the artifact
fetch layer below.

## Routing publication and rollback

Routing writes run only through `simulation-deploy.yml`, `stage12-deploy.yml`,
or `simulation-rollback.yml` in `PolicyEngine/policyengine-sim-api`. These
workflows share the repository-wide `simulation-deploy-main` concurrency group
with `cancel-in-progress: false`, which serializes publication and restoration.
The routing CLI and library check the GitHub Actions repository and caller workflow
before writing. Run the deployment or rollback workflow to change routing; local
read-only queries, `--dry-run`, and `--plan-out` remain available.

Before changing live routing, each publication keeps its plan as an artifact,
including a unique publication ID and the complete state it replaces. Recovery
and deployment-run rollback restore that state only while the corresponding
publication is still active. Manual rollback checks the plan inventory across
all run attempts before restoring the chain. A version-mode rollback instead
moves latest to an already published version and keeps other versions pinnable.
Routing writes and recovery use no lease or lock expiry wait.
For a same-version redeploy that replaced a Modal app in place, redeploy the
previous commit to recover its code; restoring routing alone cannot recover it.

Before uploading a deployment plan, the workflow records the full plan as a pending
publication in the routing store. The always-running `finalize_routing` job reads
that journal after every release gate: if all gates pass and live routing matches
the planned state, it marks the publication completed; otherwise it restores the
recorded previous state and marks it restored. Finalization uses the deployment
run ID and release environment, so failed-job reruns can finish an earlier attempt.
Artifact upload failure does not prevent recovery. `deployment_ready` requires a
successful finalizer with outcome `completed` before reporting the release green.

Until completion or restoration, new routing writes refuse to proceed. If the
finalizer's runner is lost, run `simulation-rollback.yml` with `target=beta` or
`target=prod` and `resolve_pending_publication=true`. Leave `deploy_run_id`,
`policyengine_version`, `expected_latest`, and `entrypoint_revision` blank. This
serialized recovery prints the pending plan and invokes the same finalizer with
failed gates against the target's journal, restoring even a plan that was never
applied or uploaded. It only resolves Modal routing; inspect Cloud Run traffic
separately after an interrupted promotion. If live routing is neither the plan's
previous nor planned state, recovery fails and leaves the record pending for an
operator to investigate. A finalizer for another deployment run cannot modify it.

For a completed release, use `deploy_run_id=<original GitHub run ID>` with `target`
to restore its saved artifact plans, or `policyengine_version=<published version>`
to move latest to that release. Leave `resolve_pending_publication=false` in both
modes. Read-only `--print-pending-plan --environment staging|main` also displays the
durable plan without changing routing.

## Baseline artifact pipeline (precompute → store → image fetch)

The precompute app fills a content-addressed GCS store with single-year US
datasets (2026, 2027, 2025) and the 20 per-cohort national baseline
simulations per year; the deploy bakes them into the image, and the
runtime loads baselines instead of computing them. Library logic lives in
`src/policyengine_simulation_executor/precompute.py` (keys:
`artifact_keys.py`, store client: `artifact_store.py`); the Modal app
(`src/modal/precompute_app.py`) is plumbing only.

The deploy pipeline runs it automatically: a `precompute` job in the
reusable deploy workflow fills the store on both legs before each deploy
(via `.github/scripts/modal-precompute.sh`, bucket from the repo-level
`POLICYENGINE_ARTIFACT_BUCKET` Actions variable), and the deploy
workflow's `force_recompute` dispatch input threads through as `--force`.
Manual runs remain valid for ad-hoc warming:

    export POLICYENGINE_ARTIFACT_BUCKET=<bucket-name>
    uv run modal run --env=staging src/modal/precompute_app.py

The deploy job consumes the precompute job's manifest digest:
`src/modal/app.py` resolves the manifest from GCS on the deploying
machine and passes its content as the `fetch_artifacts` image layer's
args (`src/modal/_image_setup.py`). The layer downloads the full
artifact set (~430 MB) into `POLICYENGINE_DATA_FOLDER`, sitting between
the version env and the source mount — do not reorder. Because the
manifest is content-addressed and rides in the layer args, Modal's layer
cache busts exactly when the artifact set changes and never otherwise;
there is no force-rebuild ritual. A freshness gate inside the layer
refuses to bake artifacts computed for versions other than the image's
own, and any missing store object fails the build loudly.

After the health check, each deploy leg writes a
`deployed/<environment>.json` marker (`deployed/beta.json`,
`deployed/prod.json`) recording the manifest digest, versions, and run
identity — the liveness signal for artifact garbage collection.

Local `modal deploy` runs need the same inputs the CI deploy job has:

    export POLICYENGINE_ARTIFACT_BUCKET=<bucket-name>
    export POLICYENGINE_MANIFEST_DIGEST=<from a precompute run's MANIFEST_DIGEST= line>
    # plus GCP credentials (GCP_CREDENTIALS_JSON, or gcloud ADC)

Without a digest the deploy fails at the fetch layer, by design: a
digest-less deploy can never silently ship an artifact-less image.

Operational notes:

- Idempotent by construction: artifact keys digest the full input closure
  (package versions, data content sha, certification fingerprint), so the
  run plans against the store and computes only misses. A re-run against a
  warm store is a fast no-op. Version bumps rotate the keys and trigger
  recompute automatically — there is no staleness to manage.
- Uploads are write-once, by policy, not accident: an existing store
  object is never overwritten by anything, including `--force` (which
  recomputes and re-verifies but uploads nothing for existing keys). This
  buys concurrent-warmer race safety, artifact auditability (verified
  bytes can never drift), and protection against stale-code runners
  clobbering trusted artifacts. The heal procedure for a bad artifact is
  therefore always: delete its object from the bucket, then re-run the
  precompute — deletion turns the key back into an ordinary miss, and the
  next deploy's precompute job recomputes it before the deploy leg builds
  the image.
- A determinism gate runs whenever baselines were computed: one cohort's
  uploaded artifact is compared frame-by-frame against an independent
  fresh run. The run fails if they differ.
- The final stdout line `MANIFEST_DIGEST=<digest>` names the published
  deploy manifest; the deploy pipeline consumes exactly that line.
- Only pure default requests read the baked folder: any request naming an
  explicit `data` dataset or pinning a `data_version` bypasses it (see
  `_load_dataset` in `simulation_runtime.py`).
- Known residual, inherited rather than introduced: a data re-release
  under the same revision labels leaves the cached bundle-install layer —
  and therefore the receipt every artifact key derives from — unchanged.
  That staleness class predates the artifact pipeline and lives in the
  bundle install, not the fetch.

## Independent Stage 12 segmentation and cache

The separately named Stage 12 v2 application has its own implementation under
`stage12_runtime/` and `stage12_cache/`. It does not call the v1 segmented-run
or precompute modules described above.

For an eligible US national report, the coordinator starts baseline and reform
as separate logical simulations. Each logical simulation starts 20 calls to
`run_single_simulation_segment_us`, then validates and concatenates their
dtype-preserving entity tables. This produces 40 region-group calculation
invocations per report while retaining two simulation records and two private
simulation artifacts. `segmented=false`, US subnational requests, UK requests,
cliff analysis, and labor-supply-response calculations use the
single-container Stage 12 path.

After dispatching both logical simulations, the coordinator checks both Modal
calls with nonblocking result probes on every polling pass. It does not wait
for baseline before checking reform. Both simulations share one 3,900-second
wait window, the longest simulation worker timeout, that opens when the
coordinator starts, so coordinator setup comes out of that window. The first
failed simulation supplies the report's `error_code` and safe `error_summary`.
When a simulation fails, when the window closes, or when the coordinator
itself fails after starting a simulation, the coordinator records every
started, unfinished simulation as `failed` with `simulation_cancelled` and a
summary that gives the reason, then stops it. Only a `running` record changes,
so a simulation that already succeeded or failed keeps its outcome. An
unsegmented simulation is cancelled through Modal. A segmented simulation
reads its cancelled record, cancels the region-group calls it owns, and exits
without overwriting the record; it is cancelled through Modal only if that
record could not be written. An error while reading or updating a simulation
record is logged with its identifiers and never replaces the failure that
caused it. Expected input failures retain their typed code and message.
Unexpected exceptions are reported once, in full, and persisted as their
redacted cause (type, stage, simulation role and redacted message) followed
by the same correlation ID.

Stage 12 deployments run `src/modal/stage12_precompute_app.py` against the
dedicated `STAGE12_CACHE_BUCKET`. It publishes three annual datasets plus 60
current-law US baseline files and emits
`STAGE12_CACHE_MANIFEST_DIGEST=<sha256>`. Baseline files have the names .py
saves and loads them under (`Simulation.storage_id`, which includes the SPM
selection's digest). Stored files are never replaced: a run computes only
missing files, takes no `--force`, and publishes the manifest only once a
determinism check of the planned baselines has a recorded pass. Like the v2
workers, its image and functions mount only the read-only
`pe-uk-private-hf-read-token` Hugging Face secret, which supplies
`HUGGING_FACE_TOKEN`, and
`stage12-evaluation-gcp-credentials`; the US data it reads comes from the
public `policyengine/populace-us` dataset repository. The US worker image
downloads and verifies the manifest's 63 files during image construction. The
release validation checks the recorded manifest, hashes a baseline file, and
requires a real calculation to load that file without recomputing it. Worker
validation also calculates a US household under a nonempty CTC reform using
the frontend interval format `2026-01-01.2100-12-31`. The cache manifest
digest is then stored in the separate v2 version manifest.

The cache is an optimization, not required state for a calculation: an absent
file causes a normal calculation, and a file missing planned output columns is
recalculated. Only empty-policy baseline region-group calculations against the
bundle's default dataset are eligible to read it. Reform calculations never
read a baseline file.

## Observability

`policyengine-observability` 3.x emits structured request, operation,
error, and timing logs. The deployment workflow supplies the log project and
name separately from the OpenTelemetry collector endpoint, so this service
does not embed a destination or Google Cloud identity in its source.

Modal writes structured JSON immediately to standard output and also sends it
through a bounded background queue to Cloud Logging. Modal's injected OIDC
identity is exchanged through the dedicated `modal-api-v1` Workload Identity
Federation provider, whose conditions admit only the API v1 gateway and
versioned executor application names in the approved environments.

Modal captures container output and exposes it through the app logs UI and
CLI. Useful `policyengine-observability` checks after deploying:

```bash
modal app logs policyengine-simulation-gateway --tail 100
modal app logs policyengine-simulation-gateway --tail 100 --search policyengine.observability
modal app logs policyengine-simulation-py<version> --tail 100 --search run_simulation
modal app dashboard policyengine-simulation-gateway
```

If using Modal source filters, include both standard output and standard error.
