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

## Development

- Use Python 3.13 and `uv`.
- Prefer Makefile targets when they match the task.
- For service-scoped work, run focused `uv run pytest ...` commands from the
  relevant project directory when that is faster and sufficient.
- Regenerate generated clients with `./scripts/generate-clients.sh` when API
  schemas change.

## GitHub PRs

Never open `policyengine-sim-api` PRs from forks. CI and deployment checks are
designed for same-repository branches.

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
- PR CI runs simulation unit tests, Ruff format checks, Docker build, and local
  integration tests.
- There is currently no repository-wide changelog fragment requirement.
