# Contributing to policyengine-sim-api

See the shared PolicyEngine contribution guide for cross-repo conventions. This
file covers policyengine-sim-api specifics.

## Commands

```bash
make format                # format Python source with Ruff
make check                 # Ruff check + Pyright over source directories
make test                  # service unit tests in Docker
make test-complete         # unit tests + local integration tests
make push-pr-branch        # push current branch to origin with tracking
./scripts/generate-clients.sh
```

For simulation-service-only checks:

```bash
cd projects/policyengine-simulation-executor
uv sync --extra test
uv run pytest tests/ -v
```

## Test Organisation

- Service unit tests live under each service's `tests/` directory, for example
  `projects/policyengine-simulation-executor/tests/`.
- Generated-client integration tests live under
  `projects/policyengine-apis-integ/tests/`.
- Unit tests should mock Modal, GCP, Hugging Face, and other network seams.
- Integration tests should use generated clients and clearly state any required
  service, GCP, Modal, or staging dependency.

## Opening PRs

Always create branches on the canonical repository, not a fork. The convenience
target:

```bash
make push-pr-branch
```

pushes the current branch to `origin` with the correct tracking so
`gh pr create` works. Then create and verify the PR explicitly:

```bash
gh pr create --draft --repo PolicyEngine/policyengine-sim-api --head "$(git branch --show-current)" --base main
gh pr view <PR> --repo PolicyEngine/policyengine-sim-api --json isDraft,headRepositoryOwner,headRepository
```

Before opening the PR, open or identify a GitHub issue for the work. The first
line of the PR description must be `Fixes #ISSUE_NUMBER`.

The PR is valid only if it is a draft and the head repository is
`PolicyEngine/policyengine-sim-api`. If you cannot push to that repository, ask
for access instead of opening a fork PR.

## Repo-Specific Anti-Patterns

- Do not open PRs from personal forks.
- Do not add `[codex]`, `[claude]`, `[copilot]`, or other agent labels to PR
  titles.
- Do not hand-edit generated clients without documenting why regeneration is
  not appropriate.
- Do not skip integration/client checks when changing public API schemas.
