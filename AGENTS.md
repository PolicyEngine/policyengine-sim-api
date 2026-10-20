# Agent Instructions

These instructions apply repository-wide.

## Skills System

Canonical AI-facing engineering skills live under `docs/engineering/skills/`.
Use those files as the source of truth across Codex, Claude, Copilot, and other
AI tools.

Before opening, replacing, or sharing any pull request, read
`docs/engineering/skills/github-prs.md`.

When adding, moving, or reviewing tests, read
`docs/engineering/skills/testing.md`.

Before changing correlation identifiers, telemetry transport, spans, stage
names, logging, or observability failure handling, read
`docs/engineering/skills/observability.md`.

When adding or changing a GitHub Actions workflow step or a script under
`.github/scripts/`, read `docs/engineering/skills/ci-workflows.md`.

When adding, changing, or removing a rule, a guard test, a checker
configuration, or a CI job that enforces one, read
`docs/engineering/skills/rules-registry.md`.

Before adding a Pydantic model or a class that other models inherit from, or
moving an import under `if TYPE_CHECKING:`, read
`docs/engineering/skills/typing-only-imports.md`.

## Development

- Use Python 3.13 and `uv`.
- Prefer Makefile targets when they match the task.
- For service-scoped work, run focused `uv run pytest ...` commands from the
  relevant project directory when that is faster and sufficient.
- Regenerate generated clients with `./scripts/generate-clients.sh` when API
  schemas change.

## GitHub PRs

Never open `policyengine-sim-api` PRs from forks. CI and deployment checks are
designed for same-repository branches. Agents must never bypass branch rules
(for example with `gh pr merge --admin`), even with admin credentials.

Before creating or sharing any PR, all developers and agents must:

1. Confirm the canonical repository is reachable:
   `gh repo view PolicyEngine/policyengine-sim-api --json nameWithOwner`.
2. Open a GitHub issue for the work, or verify that an appropriate issue
   already exists.
3. Put `Fixes #ISSUE_NUMBER` as the first line of the PR description, using the
   issue number from the issue created or found in the previous step.
4. Push the branch to the canonical repository, for example:
   `make push-pr-branch`.
5. Create the PR as a draft from that same repository, for example:
   `gh pr create --draft --repo PolicyEngine/policyengine-sim-api --head "$(git branch --show-current)" --base main`.
6. Verify the PR is draft and the head repository is canonical before reporting
   it:
   `gh pr view <PR> --repo PolicyEngine/policyengine-sim-api --json isDraft,headRepositoryOwner,headRepository`.

The PR is valid only if `isDraft` is `true` and the head repository is
`PolicyEngine/policyengine-sim-api`. If you cannot push to the canonical
repository, stop and ask for access. Do not create a fork PR as a fallback. If
you accidentally create one, immediately close it and replace it with a
same-repository draft PR.

## Repository Notes

- The simulation service lives in `projects/policyengine-simulation-executor`.
- API integration tests live in `projects/policyengine-apis-integ`.
- PR CI runs simulation unit tests, Ruff format checks, quality guards
  (including the rules registry), Docker build, and local integration tests.
- There is currently no repository-wide changelog fragment requirement.

## Safety Boundaries

Do not commit directly to `main`.

Do not make production deployment changes or publish clients unless the user
explicitly asks for that operation.

## Rules

<!-- BEGIN GENERATED: rules (python -m scripts.rules render) -->

Every rule for this repository lives in `rules/registry.toml` and is rendered,
with its enforcement and gaps, in `docs/generated/rules.md`. Blocking rules
fail CI. Doc-only rules are not checked automatically, so follow them yourself.
To add or change a rule, read `docs/engineering/skills/rules-registry.md`.

### Service and package boundaries

- `sim.boundaries.gateway-executor-decoupled`: The gateway and executor never import each other; they communicate only through the models and state helpers in policyengine-simulation-contract. (doc-only, not checked: follow it yourself)
- `sim.boundaries.unit-tests-need-no-services`: Unit tests never require real network credentials, Modal, Hugging Face, GCP or deployed services; those seams are mocked. (doc-only, not checked: follow it yourself)

### Stage 12 persistence

- `sim.stage12.deployment-scope`: Main-branch deploys roll out Stage 12 in both environments; a Stage 12-only deploy runs candidate tests and the requested promotion but never redeploys existing Modal resources or runs the existing stack's complete integration suite; the Stage 12 worker can request a Google OIDC token. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.stage12.deployment-uses-api-schema`: Stage 12 deployment uses the API-owned schema directly, and the Stage 12 runtime it deploys uses typed DML without raw SQL or schema changes. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.stage12.no-migration-chain`: This repository has no schema migration chain (no Alembic); Stage 12 tables are migrated by policyengine-api. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_stage12_schema_authority.py`)
- `sim.stage12.no-production-user-associations`: Stage 12 runtime modules never create production user associations (user_simulations or user_reports). (blocking: pytest `projects/policyengine-simulation-entry/tests/test_stage12_schema_authority.py`)
- `sim.stage12.no-schema-ddl`: Simulation runtimes and the Stage 12 persistence library contain no schema DDL (CREATE, ALTER, DROP or TRUNCATE of tables, types, schemas or databases, or create_all); policyengine-api owns the schema. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_stage12_schema_authority.py`)
- `sim.stage12.persistence-typed-dml-only`: The Stage 12 persistence library and runtime use typed SQLAlchemy Core DML only: no raw SQL text, driver-level SQL or schema operations. (blocking: pytest `libs/policyengine-stage12-persistence/tests/test_source_boundaries.py`)
- `sim.stage12.production-lock-has-db-runtime`: The production lock file includes the direct Stage 12 database runtime dependencies. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)

### Deployment pipeline

- `sim.deploy.container-context-minimal`: Container builds exclude local environments and unrelated projects, and the entrypoint container builds for the requested target platform. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.docker-images-build`: The simulation entry and executor Docker images build. (blocking: CI job `docker-build`)
- `sim.deploy.gcloud-without-terraform`: Deployment uses the gcloud-based workflow, not Terraform. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.image-smoke-covers-every-image-lib`: The image smoke path filter includes every library mounted into a Modal image, so a change to any of them runs the smoke. (doc-only, not checked: follow it yourself)
- `sim.deploy.modal-image-smoke`: App entrypoints import inside the real Modal images before merge, whenever a library baked into an image changes. (blocking: CI job `image-smoke`)
- `sim.deploy.numerical-sentinel-gate`: An existing-stack deployment computes the numerical release sentinel from the exact bundle it installs, and the candidate and stable services must reproduce it; a failing sentinel check fails its step. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.permission-enforcement-wired`: Simulation Entrypoint permission enforcement defaults to log-only, rejects any value other than 0 or 1, and reaches the candidate revision and every deployed authentication run. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.promotion-order-explicit`: Full-stack promotion order is explicit, and the reusable deployment rejects unexpectedly skipped phases. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.release-identity-smoke`: Deploy gates pass the whole release identity to the entrypoint smoke, which requires every expectation, refuses malformed ones, and rejects a release whose routes, latest pointers, bundle, UK model or UK dataset differ from that identity. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.revision-tags-valid`: Cloud Run revision tags are valid on a workflow's first run and fit the existing service names. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.routing-publication-journaled`: Each routing publication is named by its run, attempt and environment and journaled before it is applied; an always-running finalizer resolves it from the journal with the release-gate outcome, a release reports success only after that finalizer completes, and routing jobs never tolerate a failed command. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.shell-scripts-valid`: Deployment shell scripts have valid shell syntax. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.smoke-tests-strict`: Candidate and stable smoke tests reject a revision mismatch without retrying, and fail if the previous revision does not converge. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.staging-promotes-tested-revision`: Every staging deployment promotes its tested entrypoint revision to the staging service's stable traffic, as production deployments do, so callers of the staging stable URL reach the build that passed the staging suite. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.traffic-changes-exact`: Traffic changes target an exact, validated revision, are reversible, and refuse to run over an intervening promotion. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.deploy.workflow-shell-safety`: Cloud Run deployment never interpolates GitHub expressions into shell, escapes environment values, and pins secret versions. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)

### API contracts and integration

- `sim.contracts.clients-publish-deployed-commit`: Client publication checks out the commit that was actually deployed. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.contracts.full-integration-beta-only`: The complete integration suite runs against beta deployments only, never production. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_deployment_assets.py`)
- `sim.contracts.gateway-openapi-golden`: The gateway's OpenAPI spec matches the checked-in golden file; regenerate it deliberately when the API changes. (blocking: pytest `projects/policyengine-simulation-gateway/tests/test_openapi_golden.py`)
- `sim.contracts.local-integration`: Generated-client integration tests pass against the locally built services. (blocking: CI job `test-integration`)
- `sim.contracts.regenerate-clients`: When public API schemas change, regenerate the clients and run the generated-client integration tests. (doc-only, not checked: follow it yourself)

### Observability

- `sim.observability.failures-never-break-requests`: Invalid observability configuration fails at build or deploy time, and telemetry failures at runtime never fail a request or computation. (doc-only, not checked: follow it yourself)
- `sim.observability.header-transport-only`: observability_id travels only in the X-PolicyEngine-Observability-Id header, never in a simulation JSON body, and polling never invents one. (doc-only, not checked: follow it yourself)
- `sim.observability.id-is-diagnostic-only`: observability_id has diagnostic meaning only: never use it for idempotency, authorization, database identity, routing or business logic. (doc-only, not checked: follow it yourself)
- `sim.observability.registered-stages-only`: Span names come from the stage plans in policyengine_simulation_observability.stages; runtime code never adds span-name string literals. (doc-only, not checked: follow it yourself)

### Code quality and tests

- `sim.quality.ci-cd-scripts-ruff-exemptions`: CI/CD scripts under .github may print their output (T201); every other ruff rule applies to them. (blocking: ruff per-file ignore of T201 for `.github/**`)
- `sim.quality.command-line-scripts-ruff-exemptions`: Command-line scripts under a scripts directory may print their output (T201), run other programs (S603, S607) and use today's date (DTZ011); every other ruff rule applies to them. (blocking: ruff per-file ignore of DTZ011, S603, S607, T201 for `**/scripts/**`)
- `sim.quality.deployed-tests-ruff-exemptions`: The deployed-service test suite (projects/*/authenticated_tests, pytest run against a live candidate) gets the same ruff exemptions as unit tests (sim.quality.tests-ruff-exemptions); every other ruff rule applies to it. (blocking: ruff per-file ignore of ANN, ARG001, ARG002, ARG005, D1, DTZ001, DTZ007, INP001, PLC0415, PLR2004, S101, S102, S105, S106, S108, S301, S603, S607, SLF001, TID253 for `**/authenticated_tests/**`)
- `sim.quality.every-project-tested`: Every project and library with unit tests runs them in the PR test matrix. (doc-only, not checked: follow it yourself)
- `sim.quality.ruff-format-src`: Source code under projects/*/src and libs/*/src is formatted with ruff format. (blocking: ruff format)
- `sim.quality.ruff-format-tests`: Test code is formatted with ruff format too; CI currently checks only src directories. (doc-only, not checked: follow it yourself)
- `sim.quality.ruff-lint`: All Python code passes ruff check with every rule family selected in ruff.toml, with no ratchet; families are added one themed group at a time until the selection is ALL. (blocking: ruff check (A, ANN, ARG, ASYNC, B, BLE, C4, D, DTZ, E, E4, E7, E9, EM, ERA, EXE, F, FAST, FBT, FIX, FLY, FURB, G, I, INP, LOG, N, PD, PERF, PGH, PIE, PLC, PLW, PT, PTH, PYI, RET, RSE, RUF, S, SIM, SLF, T20, TC, TD, TID253, TRY, UP, W))
- `sim.quality.runtime-evaluated-models-resolve`: Every Pydantic model is runtime-evaluated for ruff: one of its bases, read through the imports of the model's own module, is listed in ruff.toml's runtime-evaluated-base-classes or is a runtime-evaluated class in that same module; and every Pydantic model resolves its annotations once its package is imported. (blocking: pytest `projects/policyengine-simulation-entry/tests/test_runtime_evaluated_annotations.py`, `projects/policyengine-simulation-executor/tests/test_runtime_evaluated_annotations.py`)
- `sim.quality.src-programs-ruff-exemptions`: Programs under src (the Modal deployment utilities, and the OpenAPI and Stage 12 infrastructure commands) may print their output (T201), open URLs from their arguments (S310) and, for the Modal utilities run with python -m, live in a directory that isn't a package (INP001); every other ruff rule applies to them. (blocking: ruff per-file ignore of INP001, S310, T201 for `projects/*/src/{modal/utils/*,policyengine_simulation_entry/generate_openapi,policyengine_simulation_entry/stage12_infrastructure,policyengine_simulation_gateway/generate_openapi}.py`)
- `sim.quality.tests-pass`: Each project's unit tests pass in its own locked environment. (blocking: CI job `test`)
- `sim.quality.tests-ruff-exemptions`: Test files may use pytest's bare assert, need no docstrings, annotations or named constants for magic numbers, and may reach into private members, live outside packages and import inside a test (S101, D1, ANN, PLR2004, SLF001, INP001, PLC0415). They may also run repository scripts (S603, S607) and use fake credentials (S105, S106), temporary paths (S108), naive datetimes (DTZ001, DTZ007), pickle (S301) and exec (S102), may import the lazily loaded packages at module level (TID253), and may leave arguments unused in fakes and stubs that mirror a real signature (ARG001, ARG002, ARG005). Every other ruff rule applies to tests. (blocking: ruff per-file ignore of ANN, ARG001, ARG002, ARG005, D1, DTZ001, DTZ007, INP001, PLC0415, PLR2004, S101, S102, S105, S106, S108, S301, S603, S607, SLF001, TID253 for `**/tests/**`)
- `sim.quality.unittest-ruff-exemptions`: The shared rules tool's tests use unittest, because the tool needs only the standard library, so ruff's pytest-style rules (PT) don't apply to them. (blocking: ruff per-file ignore of PT for `scripts/rules/tests/**`)

### CI workflows and scripts

- `sim.ci.multiline-run-is-script`: A GitHub Actions run: block holds one line of code and no chained commands (&&, ||, ;); anything more lives in a tested script under .github/scripts/, called from a single-line step. (blocking: quality guard `ci-scripts`)
- `sim.ci.shell-scripts-pass-shellcheck`: Every shell script under .github/scripts/ passes shellcheck. (blocking: quality guard `ci-scripts`)
- `sim.ci.workflow-scripts-tested`: Every shell script a workflow calls is run by at least one test. (blocking: quality guard `ci-scripts`)

### Process and repository hygiene

- `sim.agents.adapters-point-to-agents-md`: CLAUDE.md and .github/copilot-instructions.md only point to AGENTS.md; skills and rules are listed in AGENTS.md alone. (blocking: quality guard `rules-registry`)
- `sim.process.agents-never-bypass`: Agents never bypass branch rules, even with admin credentials: no gh pr merge --admin and no bypass option; a blocked merge is reported to a human. (doc-only, not checked: follow it yourself)
- `sim.process.pr-tracking-issue`: Every PR has a tracking issue in this repository, and the first line of its description is Fixes #ISSUE_NUMBER. (doc-only, not checked: follow it yourself)
- `sim.process.rule-machinery-code-owned`: Changes to the rule machinery (rules/, scripts/rules/, scripts/run_quality_guards.py and CODEOWNERS) need approval from a core developer other than the author. (doc-only, not checked: follow it yourself)
- `sim.process.rules-registry`: Every rule is registered in rules/registry.toml, is enforced where it claims to be, and its rendered views are current. (blocking: quality guard `rules-registry`)
- `sim.process.same-repo-draft-prs`: PRs are opened as drafts from branches in PolicyEngine/policyengine-sim-api, never from forks. (doc-only, not checked: follow it yourself)
- `sim.process.stacked-prs-full-ci-at-bottom`: In a stack of pull requests, only the lowest unmerged one runs the full PR workflow and the Modal image smoke. Every pr.yml job is needed by the CI complete gate, which fails on the pull requests above it and otherwise passes only when every job passed, so a stack merges bottom-up and nothing merges untested. (blocking: pytest `projects/policyengine-simulation-executor/tests/test_ci_complete_gate.py`)

<!-- END GENERATED: rules -->
