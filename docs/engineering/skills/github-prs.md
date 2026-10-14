# GitHub PRs

These rules apply to every developer and AI agent opening pull requests in this
repository.

## Same-Repository Draft PRs Only

Open PRs from branches in `PolicyEngine/policyengine-sim-api`, not from personal
forks. CI and deployment checks are designed around same-repository branches.

Before creating or sharing a PR:

1. Confirm the canonical repository is reachable:
   `gh repo view PolicyEngine/policyengine-sim-api --json nameWithOwner`.
2. Open a GitHub issue for the work, or verify that an appropriate issue
   already exists.
3. Put `Fixes #ISSUE_NUMBER` as the first line of the PR description, using the
   issue number from the issue created or found in the previous step.
4. Run formatting and the most relevant tests for the changed surface. If an
   expected check cannot be run, say so in the PR body.
5. Push the current branch to the canonical repository:
   `make push-pr-branch`.
6. Create the PR as a draft from that same repository:
   `gh pr create --draft --repo PolicyEngine/policyengine-sim-api --head "$(git branch --show-current)" --base main`.
7. Verify the PR is draft and the head repository is canonical:
   `gh pr view <PR> --repo PolicyEngine/policyengine-sim-api --json isDraft,headRepositoryOwner,headRepository`.
8. Leave the PR as draft unless a maintainer explicitly asks for it to be
   marked ready for review.

The PR is valid only if `isDraft` is `true` and the head repository is
`PolicyEngine/policyengine-sim-api`. If you cannot push to the canonical
repository, stop and ask for access. Do not create a fork PR as a fallback. If
you accidentally create one, close it immediately and replace it with a
same-repository draft PR.

## Branch Rules

Agents must never bypass branch rules, even when their credentials have admin
rights: do not merge with `gh pr merge --admin`, and do not use GitHub's option
to bypass rules. If a required check or review blocks a merge, report it to a
human instead.

## Stacked PRs and CI

In a stack of pull requests, only the lowest unmerged one runs the full PR
workflow (`.github/workflows/pr.yml`) and the Modal image smoke. The ones above
it run only the `CI complete` job, which fails and says so. A stack therefore
uses one set of runners instead of one per layer.

- **`CI complete` is the required check.** It needs every other job in
  `pr.yml`. On a pull request that isn't stacked, or is the lowest unmerged
  one, it passes only when every job passed. Skipped jobs would otherwise
  count as passing, so a stack can only merge bottom-up, and nothing merges
  untested.
- **When the pull request below merges,** GitHub rebases the next one onto
  the trunk, and that push starts its full run. If no run starts, push again
  (`gh stack rebase`, then `gh stack push`), or close and reopen the pull
  request. Re-running the old workflow doesn't help: it reuses the old event,
  which still describes the pull request as higher in the stack.
- **When you add a job to `pr.yml`,** add it to `CI complete`'s `needs`. If
  the job has no `needs` of its own, also give it the same stack condition as
  the other jobs.
  `projects/policyengine-simulation-executor/tests/test_ci_complete_gate.py`
  checks both.

## PR Title

Do not add `[codex]`, `[claude]`, `[copilot]`, or other agent labels to PR
titles. Use a plain descriptive title.
