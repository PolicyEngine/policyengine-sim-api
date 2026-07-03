# policyengine-simulation-executor

PolicyEngine Simulation API service.

## Modal image dependencies

The Modal images (gateway in `src/modal/gateway/app.py`, base simulation
image in `src/modal/app.py`) install from pinned requirements files under
`requirements/`, exported from the `modal-simulation-image` and
`modal-gateway-image` dependency groups in `pyproject.toml`/`uv.lock`.
Image packages therefore match the versions the test environment runs
against and can only change through a relock — never through a fresh
resolution at image-build time (issue #602 is what happens otherwise).

To change image dependencies, edit the dependency group, then run
`uv lock` and `scripts/export-modal-image-requirements.sh` (or
`make update`, which relocks and re-exports everything). CI fails if the
exports drift from the lock (`tests/test_modal_image_requirements.py`).
Note that any change to the exports invalidates the image layer cache,
including the dataset prebuild layer below.

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
