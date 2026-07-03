# policyengine-simulation-gateway

The stable Modal gateway for the simulation service: routes simulation
requests to versioned executor apps (`policyengine-simulation-py{X}`) via
the routing state in `modal.Dict`, and serves the public API contract.

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
