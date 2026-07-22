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
group or lock invalidates the image layer cache, including the dataset
prebuild layer below.

## Temporary: prebuilt single-year datasets in the Modal image

The Modal image prebuilds single-year datasets (2025–2027, US national
default only — UK deliberately excluded to keep image builds short) into
`POLICYENGINE_DATA_FOLDER` at image build time
(`src/modal/_image_setup.py:prebuild_country_datasets`), so cold containers
skip the slow runtime `ensure_datasets()` build. This is temporary until
Populace publishes single-year datasets to Hugging Face — see issue #596 for
the removal checklist; all code sites are greppable via `TEMPORARY`.

Operational notes:

- The prebuild layer runs on Modal's cloud image builder during
  `modal deploy`, and only rebuilds when `POLICYENGINE_*` versions change or
  the prebuild function itself is edited (even comment changes). It sits
  before `add_local_python_source` on purpose — do not reorder.
- The first deploy after a version bump pays the multi-hour build. Pre-warm
  it off the deploy path with the minimum-reproduction app, which builds
  only the layers up to the prebuild and then verifies the baked files and
  load path from inside a container:
  `uv run modal run --env=staging src/modal/prewarm_app.py`.
  Because it shares the layer construction with `app.py`, a successful run
  leaves the layers cached and the next real deploy fast-forwards.
- To force a rebuild of a cached layer (e.g. a data re-release under the
  same revision label), temporarily add `force_build=True` to the affected
  `run_function` call in `src/modal/app.py` for one deploy.
- Only pure default requests read the baked folder: any request naming an
  explicit `data` dataset or pinning a `data_version` bypasses it (see
  `_load_dataset` in `simulation_runtime.py`).

## Artifact precompute (baseline artifact pipeline, phase 1)

The precompute app fills a content-addressed GCS store with single-year US
datasets (2026, 2027, 2025) and the 20 per-cohort national baseline
simulations per year, so a later deploy can bake them into the image and
the runtime can load baselines instead of computing them. Library logic
lives in `policyengine_simulation_executor/precompute.py` (keys:
`artifact_keys.py`, store client: `artifact_store.py`); the Modal app
(`src/modal/precompute_app.py`) is plumbing only.

Run it manually (phase 1; a CI deploy job takes over in a later phase):

    export POLICYENGINE_ARTIFACT_BUCKET=<bucket-name>
    uv run modal run --env=staging src/modal/precompute_app.py

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
  precompute — deletion turns the key back into an ordinary miss.
- A determinism gate runs whenever baselines were computed: one cohort's
  uploaded artifact is compared frame-by-frame against an independent
  fresh run. The run fails if they differ.
- The final stdout line `MANIFEST_DIGEST=<digest>` names the published
  deploy manifest; the deploy pipeline consumes exactly that line.

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
