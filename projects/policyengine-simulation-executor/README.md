# policyengine-simulation-executor

PolicyEngine Simulation API service.

## Modal image dependencies

The executor image (`src/modal/app.py`) installs its bootstrap packages
straight from this project's `uv.lock` via
`uv_sync(frozen=True, --only-group modal-simulation-image)`. Image
packages therefore match the versions the test environment runs against
and can only change through a relock — never through a fresh resolution
at image-build time (issue #602 is what happens otherwise). Country
model packages are deliberately not in the group: the
`policyengine bundle install` layer manages them, installing into the
same interpreter (uv_sync's venv is first on PATH). The gateway lives in
its own project (`projects/policyengine-simulation-gateway`) whose image
installs the same way from that project's lock — see its README.

To change image dependencies, edit the `modal-simulation-image`
dependency group and run `uv lock`. PRs touching image inputs run an
in-image import smoke (`src/modal/smoke_app.py` via
`.github/workflows/pr-image-smoke.yml`). Note that any change to the
group or lock invalidates the image layer cache, including the artifact
fetch layer below.

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

## Observability

The service currently runs two observability backends in parallel:

- `policyengine-observability` emits structured request, operation, error,
  and runtime timing logs.
- Logfire remains enabled as the legacy platform for existing dashboards and
  alerting while we evaluate replacing it with another observability platform.

New instrumentation should be added through `policyengine-observability`; the
Logfire path is retained for continuity during that evaluation.

For `policyengine-observability`, this service intentionally forces:

- `log_destinations=("stdout",)`
- `otel_enabled=False`
- `google_cloud_project=None`

Cloud Logging and OTel export are therefore disabled until the target GCP
project is ready. The package does not currently provide memory-usage
measurements, so memory is not emitted.

Modal captures container output and exposes it through the app logs UI and
CLI. Useful `policyengine-observability` checks after deploying:

```bash
modal app logs policyengine-simulation-gateway --tail 100
modal app logs policyengine-simulation-gateway --tail 100 --search policyengine.observability
modal app logs policyengine-simulation-py<version> --tail 100 --search run_simulation
modal app dashboard policyengine-simulation-gateway
```

If using Modal source filters, include both `stdout` and `stderr`. The
observability destination is named `stdout`, but its current Python logging
handler writes through the standard stream handler.

Logfire continues to use the `policyengine-logfire` Modal secret. Worker
functions and the gateway configure Logfire only when `LOGFIRE_TOKEN` is
present.
