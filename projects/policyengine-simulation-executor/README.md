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
read-only queries, `--dry-run`, `--plan-out`, and `--plan-latest-out` remain
available.

Before changing live routing, each publication keeps its plan as an artifact,
including a unique publication ID and the complete state it replaces. Recovery
and deployment-run rollback restore that state only while the corresponding
publication is still active. Manual rollback checks the plan inventory across
all run attempts before restoring the chain. A version-mode rollback instead
moves latest to an already published version and keeps other versions pinnable.
Routing writes and recovery use no lease or lock expiry wait.
For a same-version redeploy that replaced a Modal app in place, redeploy the
previous commit to recover its code; restoring routing alone cannot recover it.

A deployment publishes routing twice (rule `sim.deploy.latest-moves-after-checks`):

1. **Before its checks**, `update_routing` adds the release's routes (its
   policyengine.py version and its US and UK model versions, mapped to the
   deployed app) and its bundle manifest, and leaves `latest` alone. The
   checks pin those versions, so they reach the release through the gateway,
   while unpinned requests still reach the previous release. Requests that
   pin one of the release's US or UK model versions without a policyengine.py
   version reach the release from this point, including a model version it
   shares with the previous release. The job completes its own publication,
   so a failed check leaves the routes in place, and those requests keep
   reaching the failed release until a rerun succeeds or a rollback with
   `deploy_run_id` removes its routes.

   The job publishes only into a stored routing state that is already valid
   with a complete `latest` (a routed policyengine.py, US and UK version), or
   into a missing or empty one: the first publication, which makes the
   release latest (if the gateway was resolving through the legacy version
   Dicts, those requests move to the release at this step). Before
   publishing anything, it refuses any other stored state, which an operator
   must repair, and refuses to reroute `latest`'s policyengine.py version to
   the release when that version's stored route names another app.
2. **After every check and the Cloud Run promotion**, `publish_latest` moves
   `latest` to the release when it is newer (or with `force_latest`), checks
   with the entrypoint smoke that the release is routed and, when it is now
   latest, that unpinned requests reach it, and completes its publication. If
   a step after recording fails, it restores the state it replaced; the Cloud
   Run promotion, which ran before it, stays. It journals a new state even
   when `latest` stays, so each attempt that reaches it replaces an earlier
   attempt's outcome.

No job that runs after `update_routing` is upstream of it, so "Re-run failed
jobs" after a failed check reruns the failed checks, `publish_latest` and the
finalizer without republishing the routes.

Before uploading each plan, its job records the full plan as a pending
publication in the routing store. The always-running `finalize_routing` job reads
that journal after every release check and `publish_latest`, and resolves a
publication that a failed or cancelled job left pending: if all checks pass and
live routing matches the planned state, it marks the publication completed;
otherwise it restores the recorded previous state and marks it restored. When
nothing is pending it reports the outcome this run last recorded. Finalization
uses the deployment run ID and release environment, so failed-job reruns can
finish an earlier attempt. Artifact upload failure does not prevent recovery.
`deployment_ready` requires `publish_latest` to succeed and a successful
finalizer with outcome `completed` before reporting the release green.

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
to restore its saved artifact plans (each attempt's routes plan,
`routing-plan-<target>-<attempt>`, and latest plan,
`routing-latest-plan-<target>-<attempt>`), or
`policyengine_version=<published version>` to move latest to that release.
GitHub stops listing an attempt's artifact once the job that uploaded it
reruns; the restore accepts such a missing plan only when the plans published
before and after it show its change undone, and otherwise refuses rather than
stopping short. Leave `resolve_pending_publication=false` in both
modes. Read-only `--print-pending-plan --environment staging|main` also displays the
durable plan without changing routing.

## Stage 12 v2 default version

Stage 12 keeps its own manifest of deployed v2 workers, in the separate
`simulation-api-v2-version-manifest` Modal Dict, and the Simulation Entrypoint
resolves each Stage 12 report through the manifest's `default_version` unless
`STAGE12_V2_WORKER_VERSION` pins one. A deployment that publishes Stage 12
changes the manifest twice (rule `sim.deploy.stage12-default-moves-after-checks`),
with its own mechanism, separate from the v1 routing jobs above:

1. **Before its checks**, `publish_stage12_v2_manifest` adds the release's
   worker with `update_v2_version_manifest --keep-default`, leaving the
   default version as it is. It refuses a manifest that is missing (see
   below). Stage 12
   reports keep reaching the previous worker. No check that runs after this
   publication is upstream of this job, so "Re-run failed jobs" after a failed
   check or default move never republishes the worker.
2. **After every check and the Cloud Run promotion**,
   `publish_stage12_v2_default` moves the default to the release's worker
   when it is newer (`--move-default`), keeping a plan of the manifest before
   and after; checks, from the runner, that every key holds what it wrote and
   that a `V2ManifestLoader` (the reader the entrypoint uses) resolves the
   plan's default (`--verify-plan`); and, if a later step
   fails or the run is cancelled, writes back the manifest it replaced, only
   while the stored manifest is still what it wrote (`--restore-plan`).
   `deployment_ready` requires this job to succeed whenever Stage 12 was
   requested. This job does not wait for the v1 `publish_latest` job, so the
   Stage 12 default can move even when v1 `latest` does not; `deployment_ready`
   then fails the deployment.

The v2 worker's Modal app is named after the policyengine.py version alone, so
a deployment that does not change that version redeploys the current worker's
app in place, before the checks; the ordering above applies to a deployment
that changes it.

The manifest is three keys, one copy for each generation of reader, written
one after another, so a write that stops part-way leaves some keys on the
previous manifest. The move refuses to start unless all three hold the same
manifest, the check reads all three, and the restore recognises a part-way
write of either the move or an earlier restore and writes the starting
manifest back to every key. Modal expires a Dict entry after 7 days without a
read or write, and current readers read the oldest key only when neither newer
key exists, so it can expire between deployments. A write never deletes a key,
so the move, check and restore accept a missing older key; a move or restore
that writes the manifest writes it again, and otherwise the next deployment's
worker publication does. The newest key is required. A move that leaves the
default where it is writes nothing, so the manifest's digest, which Stage 12
report identities include, stays the same. A release with a newer worker
changes the digest twice, at the worker publication and at the move, so Stage
12 reports created between the two get identities with the intermediate
digest.

The entrypoint reads the manifest only for a Stage 12 request, and Stage 12 is
off in beta, so without other reads a week with no deployment would let all
three keys expire. The scheduled workflow `stage12-v2-manifest-refresh.yml`
(rule `sim.deploy.stage12-manifest-read-daily`) reads every key once a day in
each Modal environment a deployment publishes the manifest to (`staging` and
`main`), with `update_v2_version_manifest --refresh`, and fails when there is
no manifest. GitHub notifies only whoever last changed a scheduled workflow's
schedule of a failed run, and disables schedules in a public repository after
60 days without activity; either way, a deployment then refuses the missing
manifest as described next.

A missing manifest, never published or expired, looks the same to a
deployment, and publishing into it would make the release's worker the default
before the checks and leave out every earlier worker. So the deployment's
worker publication refuses it, before the checks, and opens the Dict without
creating it. The integration tests need that publication, so the refusal holds
up the whole beta deployment, and prod after it, even where Stage 12 is off,
until the manifest is recovered. To recover, an operator, with Modal credentials for the workspace
(their own token; the deployment uses the repository secrets `MODAL_TOKEN_ID`
and `MODAL_TOKEN_SECRET`), from `projects/policyengine-simulation-executor`,
while no deployment is running:

1. Confirms the manifest is missing: `uv run --frozen python -m
   src.modal.utils.update_v2_version_manifest --environment <modal environment>
   --refresh` reports "there is no v2 manifest", as it also does when the Dict
   itself does not exist. Any other error is not a missing manifest.
2. Starts the manifest with the worker that was Stage 12's default before the
   failed deployment, so that the failed deployment's own default move still
   waits for its checks. That worker is the `default_version` of the `after`
   manifest in the newest `stage12-v2-default-plan-<release environment>-<attempt>`
   artifact, or of its `before` manifest if that run's restore step reported
   "restored" (kept 30 days; past that, the last deployment whose "Move the
   Stage 12 v2 default to the release after every check" job succeeded). The
   worker's `validation.validation_invocation_id` and `validated_at` in that
   manifest identify the deployment that published it, where several kept
   the same policyengine.py version. Use
   the release environment that deploys to the Modal environment being
   recovered: `beta` for `staging`, `prod` for `main`. From the commit of the
   deployment that published that worker, after `uv sync --frozen`, with that
   deployment's `stage12-worker-validation-<release environment>` artifact, run
   `uv run --frozen python -m src.modal.utils.update_v2_version_manifest
   --environment <modal environment> --validation-file <validation.json>
   --first-publication`; on a commit from before `--first-publication`
   existed, leave that option out, which starts a missing manifest the same
   way but does not refuse an existing one. If the failed deployment keeps
   that policyengine.py version, its worker app has already replaced that one
   in place, so use the failed deployment's commit and artifact instead. In a
   new Modal environment there is no earlier worker, and the failed
   deployment's own worker starts the manifest. `--first-publication` refuses
   a manifest that exists.
3. Reruns the failed jobs.

Earlier workers' entries are not restored, so a request that pins one of them
through `STAGE12_V2_WORKER_VERSION` fails until it is published again.

The plan is kept as the `stage12-v2-default-plan-<environment>-<attempt>`
artifact whenever the move step ran, including a move that stopped part-way;
`<environment>` is the GitHub release environment, and `beta` deploys to the
Modal environment `staging`, `prod` to `main`. The move step adds the
manifest's digests before and after the move, and whether requests that pin no
v2 version now reach the release's worker, to the run's summary page.
If the restore step itself fails, an operator downloads that attempt's artifact
and, from `projects/policyengine-simulation-executor`, runs
`uv run --frozen python -m src.modal.utils.update_v2_version_manifest
--environment <modal environment> --restore-plan <plan.json>` with Modal
credentials for the workspace (the deployment uses the repository secrets
`MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`). Do this before rerunning the job or
deploying again. A rerun's move refuses keys that hold different manifests, and
its own restore has no plan from the earlier attempt. The next deployment's
worker publication reads the manifest the way a reader does and rewrites every
key from that copy. After a move that wrote only the newest key, that drops
the region datasets of every worker already in the manifest; after one that
wrote the two newest, the failed release stays the default until that
deployment's own move. No workflow
restores an earlier default after a completed deploy, and the deployment's
`force_latest` input does not move the v2 default. Only an operator's
publication (`--first-publication`, or `--validation-file` alone) creates the
manifest Dict, so a deployment's publication, a move, check, restore or refresh
against a Modal environment without one fails instead of creating an empty Dict
there.

If the runner is lost after the move starts writing and before the plan is
uploaded, no plan is kept and no restore runs. The default then stays on the
release, which passed every check, and the deployment fails. "Re-run failed
jobs" finds the default already on the release, writes nothing and succeeds,
unless the write stopped part-way: then the rerun's move refuses the keys, and
an operator repairs them by hand.

## Baseline artifact pipeline (precompute → store → image fetch)

The precompute app fills a content-addressed GCS store with single-year US
datasets (2026, 2027, 2025), the 20 per-cohort national baseline
simulations per year, and per-state datasets: for each state region whose
requests load a dataset other than the bundle default (the ACS-local
dataset since policyengine 6.7), that dataset's year file filtered to the
state's households, one file per state and year. The deploy bakes them into
the image, and the runtime loads baselines instead of computing them. Library logic lives in
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
artifact set into `POLICYENGINE_DATA_FOLDER`: about 430 MB of national
datasets and baselines, plus several GB of per-state datasets, each placed
in `state-datasets/<dataset>/<state>/` under the year file's own name. It
sits between
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
- A US state or congressional-district request whose region loads a
  non-default dataset reads its state's baked file when the image has one for
  the requested year and .py's year-file reuse checks accept it (see
  `state_datasets.baked_state_data_folder`). Otherwise it prepares the full
  regional dataset during the request, as before. The run records which in
  its `dataset_source` attribute: `baked_state` or `on_demand`.
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
dedicated `STAGE12_CACHE_BUCKET`. It publishes three annual datasets, 60
current-law US baseline files and, for each state region whose requests load
a dataset other than the bundle default, that dataset's year file filtered to
the state's households (one per state and year), and emits
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
downloads and verifies the manifest's files during image construction: the 63
national files into the data folder, and each state dataset into
`state-datasets/<dataset>/<state>/` under the year file's own name. The
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
