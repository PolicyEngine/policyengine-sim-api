# policyengine-api-simulation

PolicyEngine Simulation API service.

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
- The first deploy after a version bump pays the multi-hour build; consider
  pre-warming with a scratch `MODAL_APP_NAME` deploy before merging.
- To force a rebuild of a cached layer (e.g. a data re-release under the
  same revision label), temporarily add `force_build=True` to the affected
  `run_function` call in `src/modal/app.py` for one deploy.
- Requests pinning a non-default `data_version` bypass the baked folder
  (see `_load_dataset` in `simulation_runtime.py`).