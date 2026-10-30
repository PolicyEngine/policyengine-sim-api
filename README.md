# policyengine-sim-api

Monorepo for PolicyEngine's simulation API — the services, shared libraries, and
deployment configuration behind PolicyEngine's economic simulation service. The
permanent public entrypoint runs on Cloud Run. During the current migration it
authenticates callers and delegates to the existing Modal gateway, which
continues to route requests to versioned simulation executor apps.

## Architecture

Active services (`projects/`):

- **policyengine-simulation-entry** — the permanent Cloud Run entrypoint. It
  owns caller authentication and currently proxies the public contract to the
  existing Modal gateway with a separate machine identity.
- **policyengine-simulation-gateway** — the existing Modal routing gateway.
  During migration it remains the entrypoint's upstream and routes each request
  to a versioned executor app (`policyengine-simulation-py{X}`) using routing
  state in a `modal.Dict`.
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

`make dev` runs it in the foreground with a live-reload build. The gateway is
not run locally — it is a Modal-only service. The Cloud Run entrypoint can be
run and tested independently from `projects/policyengine-simulation-entry`.

## Testing

```bash
make test                           # unit tests for all projects and libs (uv)
make test-integration-with-services # start services + run integration tests
make test-complete                  # unit + integration (manages services)
```

Each project's unit tests run in its own locked uv environment — the same lock
used by its deployed image. Integration tests generate the API client, start
the executor via Docker Compose, wait for
`http://localhost:8082/ping/alive`, then run `projects/policyengine-apis-integ`
against it.

Other useful targets:

```bash
make format           # format with ruff
make check            # ruff check + pyright
make generate-clients # regenerate the OpenAPI Python client
```

## Deployment

Simulation workers and the old gateway continue to deploy to Modal. The
permanent Simulation Entrypoint deploys to Cloud Run as tagged no-traffic
candidates. Beta qualification uses Cloud Run's generated candidate URLs and
does not require a custom beta domain.

A merge to `main` doesn't deploy by itself. Its push runs only the `versioning`
job of `.github/workflows/simulation-deploy.yml`, which raises `VERSION`, builds
the merge's section of `CHANGELOG.md` from the fragments in `changelog.d/`, and
pushes one release commit, `Update PolicyEngine simulation API`. That commit's
push deploys one complete stack at a time (see
`docs/engineering/skills/github-prs.md#how-a-merge-is-released-and-deployed`):

1. prepare the environment's deployment inputs and runtime secrets;
2. deploy and unit test the Entrypoint, Modal gateway, and Modal executor in
   three parallel service jobs;
3. publish routing only after all three service jobs pass;
4. run generated-client and live authentication tests through the complete
   three-service request path;
5. assign the environment's stable traffic to the exact tested entrypoint
   revision after that environment's tests pass, in `beta` as in `prod`
   (`beta` only for a run from `main`);
6. verify the stable endpoint, restoring its previous revision if immediate
   post-promotion checks fail; and
7. begin the `prod` deployment only after every `beta` deployment,
   qualification, promotion and revision clean-up job succeeds, so a failed
   staging clean-up also stops the production deployment.

The staging service's stable URL therefore serves the revision that passed the
staging tests, so staging callers of that URL, such as the Public API's staging
service, reach the build the deployment tested. After each successful
promotion, the deployment deletes the service's older revisions, keeping only
the promoted revision and the one it replaced, for rollback.

A successful deployment of a release commit publishes the API client from the
exact deployed commit. A manual dispatch of the workflow deploys without a
release and publishes no client.

The stable production service is exposed through a separately managed global
external HTTPS load balancer and canonical hostname. Domain ownership, DNS,
TLS, load-balancer resources, and other one-time cloud setup remain operator
responsibilities outside committed deployment automation. When the canonical
hostname is supplied as protected environment metadata, every production
promotion verifies it after checking the generated stable service URL.
Entrypoint traffic promotion does not change API v1 revision traffic or its
simulation-entrypoint migration flag.

The stable gateway app is `policyengine-simulation-gateway`; executors deploy as
versioned `policyengine-simulation-py{version}` apps. For Modal image and deploy
specifics (image dependency pinning, artifact fetch, observability), see the
service READMEs:

- `projects/policyengine-simulation-entry/README.md`
- `projects/policyengine-simulation-gateway/README.md`
- `projects/policyengine-simulation-executor/README.md`

## Project structure

```
projects/
  policyengine-simulation-entry/      # permanent Cloud Run public entrypoint
  policyengine-simulation-gateway/    # existing Modal routing gateway
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
.github/workflows/                    # CI, Cloud Run/Modal deploy, client publishing
```

## Contributing

See [AGENTS.md](AGENTS.md) and
[docs/engineering/skills/github-prs.md](docs/engineering/skills/github-prs.md)
for the canonical same-repository draft PR workflow.

1. Create a feature branch (never commit to `main`).
2. Open a GitHub issue for the work; put `Fixes #ISSUE_NUMBER` as the first
   line of the PR description. Every PR needs one: the `PR description` check
   fails otherwise.
3. Make changes and run the relevant tests locally.
4. Open a same-repository draft PR: `make push-pr-branch`, then
   `gh pr create --draft`.
5. Wait for CI checks to pass.
