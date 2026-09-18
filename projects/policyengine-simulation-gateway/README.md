# policyengine-simulation-gateway

The stable Modal gateway for the simulation service: routes simulation
requests to versioned executor apps (`policyengine-simulation-py{X}`) via
the routing state in `modal.Dict`, and serves the public API contract.

## Dataset selection

The economy comparison and budget-window endpoints use the certified dataset
from the selected PolicyEngine.py release. They reject request fields `data`
and `data_version` with HTTP 422, including when either field is null. The
response still reports the dataset and data version used. Use `region` or
`region_group` to select a geographic subset; the worker resolves the
appropriate dataset for that region from its release bundle.

## Country-version routing

A request that names a country model `version` resolves to the app the
routing state lists for it. When the registry ties that app to exactly one
wrapper bundle and the bundle's manifest states a *different* model version
for the country, the gateway refuses the request with a 400 instead of
routing it.

That is a deliberate change from the pre-canonical gateway, which served
the route and returned a self-contradictory body — `version` from the stale
route, `policyengine_bundle.model_version` from the live manifest. It can
happen without anyone doing anything wrong: publishing only ever *adds*
country routes and overwrites the bundle manifest for the wrapper it
deploys, and nothing prunes the old country route, so re-publishing one
wrapper with an upgraded country model leaves the previous country version
pointing at an app that no longer serves it. Callers pinning an old
`version` (the `policyengine-apis-integ` suite pins one) should move to a
version the current bundle states, or pass `policyengine_version`
explicitly.

A manifest that states *no* model version for the country — entry absent,
or the value absent, non-string, empty or whitespace — contradicts nothing
and still routes.

## Image dependencies

The Modal image installs with `uv_sync(frozen=True)` from this project's
`uv.lock`, so the image environment is exactly what CI unit tests run
against — packages can only change through a relock (see issue #602 for
what fresh build-time resolution caused). Local packages (this package,
the contract/observability libs, policyengine-fastapi) are dev-group path
dependencies and ship into the image as mounted source; the image build
passes `--no-default-groups` so the dev group never installs in Modal's
build context.

## Common tasks

```bash
uv sync --extra test && uv run pytest          # unit tests (parity env)
uv run modal deploy --env=staging src/policyengine_simulation_gateway/app.py
uv run modal run --env=staging src/policyengine_simulation_gateway/smoke_app.py  # in-image import smoke
uv run python -m policyengine_simulation_gateway.generate_openapi
../../scripts/generate-clients.sh              # regen client for apis-integ
```

PRs touching image inputs run the smoke automatically
(`.github/workflows/pr-image-smoke.yml`).

The generated client package name stays `policyengine_api_simulation_client`
(external consumers depend on it; see `openapi-python-client.yaml`).
