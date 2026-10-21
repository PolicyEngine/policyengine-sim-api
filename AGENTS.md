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

When changing code that aggregates simulation microdata (the output builders,
the national reduce, Stage 12 aggregation, or anything that reads
`output_dataset.data`), read `docs/engineering/skills/microdata-weights.md`.

Before adding code that connects to a database, declares tables, or changes a
database schema, read `docs/engineering/skills/database-access.md`.

When adding or changing a GitHub Actions workflow step or a script under
`.github/scripts/`, read `docs/engineering/skills/ci-workflows.md`.

When adding, changing, or removing a rule, a guard test, a checker
configuration, or a CI job that enforces one, read
`docs/engineering/skills/rules-registry.md`.

Before adding a Pydantic model or a class that other models inherit from, or
moving an import under `if TYPE_CHECKING:`, read
`docs/engineering/skills/typing-only-imports.md`.

Before changing a package's Pyright settings, a Pyright baseline or the pinned
Pyright version, when the Pyright check fails, or when adding a package, read
`docs/engineering/skills/type-checking.md`.

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
- PR CI runs simulation unit tests, Ruff format and lint checks, Pyright for
  every package, quality guards (including the rules registry), Docker build,
  and local integration tests. A separate `PR description` job checks that
  every open PR's description starts with `Fixes #ISSUE_NUMBER` for an open
  issue in this repository.
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

- Checked: `sim.boundaries.sqlalchemy-and-alembic-only`
- `sim.boundaries.gateway-executor-decoupled`: The gateway and executor never import each other; they communicate only through the models and state helpers in policyengine-simulation-contract. (doc-only, not checked: follow it yourself)
- `sim.boundaries.unit-tests-need-no-services`: Unit tests never require real network credentials, Modal, Hugging Face, GCP or deployed services; those seams are mocked. (doc-only, not checked: follow it yourself)

### Stage 12 persistence

- Checked: `sim.stage12.deployment-scope`, `sim.stage12.deployment-uses-api-schema`, `sim.stage12.no-migration-chain`, `sim.stage12.no-production-user-associations`, `sim.stage12.no-schema-ddl`, `sim.stage12.persistence-typed-dml-only`, `sim.stage12.production-lock-has-db-runtime`

### Deployment pipeline

- Checked: `sim.deploy.container-context-minimal`, `sim.deploy.docker-images-build`, `sim.deploy.gcloud-without-terraform`, `sim.deploy.modal-image-smoke`, `sim.deploy.numerical-sentinel-gate`, `sim.deploy.permission-enforcement-wired`, `sim.deploy.promotion-order-explicit`, `sim.deploy.release-identity-smoke`, `sim.deploy.revision-tags-valid`, `sim.deploy.routing-publication-journaled`, `sim.deploy.shell-scripts-valid`, `sim.deploy.smoke-tests-strict`, `sim.deploy.traffic-changes-exact`, `sim.deploy.workflow-shell-safety`
- `sim.deploy.image-smoke-covers-every-image-lib`: The image smoke path filter includes every library mounted into a Modal image, so a change to any of them runs the smoke. (doc-only, not checked: follow it yourself)

### API contracts and integration

- Checked: `sim.contracts.clients-publish-deployed-commit`, `sim.contracts.full-integration-beta-only`, `sim.contracts.gateway-openapi-golden`, `sim.contracts.local-integration`
- `sim.contracts.regenerate-clients`: When public API schemas change, regenerate the clients and run the generated-client integration tests. (doc-only, not checked: follow it yourself)

### Observability

- `sim.observability.failures-never-break-requests`: Invalid observability configuration fails at build or deploy time, and telemetry failures at runtime never fail a request or computation. (doc-only, not checked: follow it yourself)
- `sim.observability.header-transport-only`: observability_id travels only in the X-PolicyEngine-Observability-Id header, never in a simulation JSON body, and polling never invents one. (doc-only, not checked: follow it yourself)
- `sim.observability.id-is-diagnostic-only`: observability_id has diagnostic meaning only: never use it for idempotency, authorization, database identity, routing or business logic. (doc-only, not checked: follow it yourself)
- `sim.observability.registered-stages-only`: Span names come from the stage plans in policyengine_simulation_observability.stages; runtime code never adds span-name string literals. (doc-only, not checked: follow it yourself)

### Weighted microdata

- Checked: `sim.microdata.builder-tests-use-weighted-frames`, `sim.microdata.weight-stripping-confined`, `sim.microdata.weights-not-aggregated-as-values`
- `sim.microdata.weights-not-values`: Microdata columns are weights, values, labels or counts: weights are only summed plainly to count entities or passed as weights=, values are aggregated with their own entity's weights, labels only group or filter, and counts derive from weights. (doc-only, not checked: follow it yourself)

### Code quality and tests

- Checked: `sim.quality.ci-cd-scripts-ruff-exemptions`, `sim.quality.command-line-scripts-ruff-exemptions`, `sim.quality.deployed-tests-ruff-exemptions`, `sim.quality.every-package-type-checked`, `sim.quality.pyright-apis-integ`, `sim.quality.pyright-executor`, `sim.quality.pyright-gateway`, `sim.quality.pyright-libraries-and-entry`, `sim.quality.ruff-format-src`, `sim.quality.ruff-lint`, `sim.quality.runtime-evaluated-models-resolve`, `sim.quality.src-programs-ruff-exemptions`, `sim.quality.tests-pass`, `sim.quality.tests-ruff-exemptions`, `sim.quality.unittest-ruff-exemptions`
- `sim.quality.every-project-tested`: Every project and library with unit tests runs them in the PR test matrix. (doc-only, not checked: follow it yourself)
- `sim.quality.ruff-format-tests`: Test code is formatted with ruff format too; CI currently checks only src directories. (doc-only, not checked: follow it yourself)

### Dependency locking

- Checked: `sim.dependencies.install-from-lock`, `sim.dependencies.lock-committed`, `sim.dependencies.scheduled-relock`, `sim.dependencies.test-from-lock`

### CI workflows and scripts

- Checked: `sim.ci.multiline-run-is-script`, `sim.ci.shell-scripts-pass-shellcheck`, `sim.ci.workflow-scripts-tested`

### Process and repository hygiene

- Checked: `sim.agents.adapters-point-to-agents-md`, `sim.process.pr-tracking-issue`, `sim.process.rules-registry`, `sim.process.stacked-prs-full-ci-at-bottom`
- `sim.process.agents-never-bypass`: Agents never bypass branch rules, even with admin credentials: no gh pr merge --admin and no bypass option; a blocked merge is reported to a human. (doc-only, not checked: follow it yourself)
- `sim.process.rule-machinery-code-owned`: Changes to the rule machinery (rules/, scripts/rules/, scripts/run_quality_guards.py and CODEOWNERS) need approval from a core developer other than the author. (doc-only, not checked: follow it yourself)
- `sim.process.same-repo-draft-prs`: PRs are opened as drafts from branches in PolicyEngine/policyengine-sim-api, never from forks. (doc-only, not checked: follow it yourself)

<!-- END GENERATED: rules -->
