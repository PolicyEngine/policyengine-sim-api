# policyengine-sim-api

Monorepo for PolicyEngine's simulation API — the services, shared libraries, and
deployment configuration behind PolicyEngine's economic simulation service. The
public API is served by a stable Modal gateway that routes requests to versioned
simulation executor apps.

## Architecture

Active services (`projects/`):

- **policyengine-simulation-gateway** — the stable, public Modal gateway. Serves
  the API contract and routes each request to a versioned executor app
  (`policyengine-simulation-py{X}`) using routing state in a `modal.Dict`.
- **policyengine-simulation-executor** — the simulation engine. Runs on Modal as
  versioned apps, and also builds a Docker image used for local development and
  integration tests (port 8082).
- **policyengine-apis-integ** — integration tests that exercise the generated API
  client against a running executor.

Shared libraries (`libs/`):

- **policyengine-fastapi** — shared FastAPI utilities and observability contracts.
- **policyengine-simulation-contract** — the gateway↔executor request/response contract.
- **policyengine-simulation-observability** — observability helpers for the services.

`projects/policyengine-api-full` and `projects/policyengine-api-tagger` are
reserved placeholder stubs (no implementation yet).

The generated Python client is published to PyPI as
`policyengine_api_simulation_client` for external consumers.

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- Docker and Docker Compose (for the executor container and integration tests)

## Local development

The simulation executor runs locally via Docker Compose on port 8082:

```bash
make up        # start the simulation-executor (http://localhost:8082) in the background
make logs      # tail logs
make down      # stop
```

`make dev` runs it in the foreground with a live-reload build. The gateway is not
run locally — it is a Modal-only service.

## Testing

```bash
make test                           # unit tests for all projects and libs (uv)
make test-integration-with-services # start services + run integration tests
make test-complete                  # unit + integration (manages services)
```

Each project's unit tests run in its own locked uv environment — the same lock
the Modal image installs with `uv_sync(frozen=True)`. Integration tests generate
the API client, start the executor via Docker Compose, wait for
`http://localhost:8082/ping/alive`, then run `projects/policyengine-apis-integ`
against it.

Other useful targets:

```bash
make format           # format with ruff
make check            # ruff check + pyright
make generate-clients # regenerate the OpenAPI Python client
```

## Deployment

Deployment targets Modal and is automated through GitHub Actions — there is no
manual deploy step. On merge to `main`, the Modal deploy workflow
(`.github/workflows/modal-deploy.yml`):

1. Deploys to the beta (staging) Modal environment and runs integration tests.
2. Deploys to the production Modal environment and runs integration tests.
3. Publishes the API client to PyPI (`.github/workflows/publish-clients.yml`).

The stable gateway app is `policyengine-simulation-gateway`; executors deploy as
versioned `policyengine-simulation-py{version}` apps. For Modal image and deploy
specifics (image dependency pinning, dataset prebuild, observability), see the
service READMEs:

- `projects/policyengine-simulation-gateway/README.md`
- `projects/policyengine-simulation-executor/README.md`

## Project structure

```
projects/
  policyengine-simulation-gateway/    # stable Modal gateway (public API)
  policyengine-simulation-executor/   # simulation engine (versioned Modal apps)
  policyengine-apis-integ/            # integration tests
  policyengine-api-full/              # reserved stub
  policyengine-api-tagger/            # reserved stub
libs/
  policyengine-fastapi/
  policyengine-simulation-contract/
  policyengine-simulation-observability/
deployment/
  docker-compose.yml                  # local simulation-executor
scripts/                              # client generation + local integration helpers
.github/workflows/                    # CI, Modal deploy, client publishing
```

## Contributing

See [AGENTS.md](AGENTS.md) and
[docs/engineering/skills/github-prs.md](docs/engineering/skills/github-prs.md)
for the canonical same-repository draft PR workflow.

1. Create a feature branch (never commit to `main`).
2. Open a GitHub issue for non-trivial work; put `Fixes #ISSUE_NUMBER` as the
   first line of the PR description.
3. Make changes and run the relevant tests locally.
4. Open a same-repository draft PR: `make push-pr-branch`, then
   `gh pr create --draft`.
5. Wait for CI checks to pass.
