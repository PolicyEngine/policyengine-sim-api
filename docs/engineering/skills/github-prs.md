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

## PR Title

Do not add `[codex]`, `[claude]`, `[copilot]`, or other agent labels to PR
titles. Use a plain descriptive title.
