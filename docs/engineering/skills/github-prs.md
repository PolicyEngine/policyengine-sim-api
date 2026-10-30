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
   issue number from the issue created or found in the previous step. The
   `PR description` check fails otherwise; see [The `Fixes` Line](#the-fixes-line).
4. Add a changelog fragment, `changelog.d/<name>.<type>.md`. The
   `Changelog fragment` check fails otherwise; see
   [Changelog Fragments](#changelog-fragments).
5. Run formatting and the most relevant tests for the changed surface. If an
   expected check cannot be run, say so in the PR body.
6. Push the current branch to the canonical repository:
   `make push-pr-branch`.
7. Create the PR as a draft from that same repository:
   `gh pr create --draft --repo PolicyEngine/policyengine-sim-api --head "$(git branch --show-current)" --base main`.
8. Verify the PR is draft and the head repository is canonical:
   `gh pr view <PR> --repo PolicyEngine/policyengine-sim-api --json isDraft,headRepositoryOwner,headRepository`.
9. Leave the PR as draft unless a maintainer explicitly asks for it to be
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
it run only the `CI complete` job, which fails and says so, and the
`PR description` job, which every open pull request runs. A stack therefore
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

## The `Fixes` Line

Rule `sim.process.pr-tracking-issue`. The `PR description` job, in
`.github/workflows/pr-description.yml`, checks every open pull request whenever
it is opened, reopened or edited and on every push, including each pull request
in a stack. It skips closed pull requests. It reads the description from the
event that started the run and fails unless:

- the first line is exactly `Fixes #ISSUE_NUMBER`;
- `ISSUE_NUMBER` is an open issue in this repository. A pull request's number,
  an issue that was moved to another repository, a closed issue and a number
  with no issue all fail.

CRLF line endings, which the GitHub web editor uses, and trailing spaces are
fine. These fail:

- an empty description;
- a blank first line, or leading spaces before `Fixes`;
- other wording, such as `Fixes #615.`, `Closes #12` or `Refs #12`;
- a reference to another repository, such as `Fixes PolicyEngine/policyengine-api#12`.

To fix a failure:

- **Wrong first line:** edit the description. Saving the edit runs the check
  again.
- **Closed issue:** if the pull request still fixes it, reopen the issue, then
  re-run the job from the pull request's Checks tab. Reopening an issue starts
  no run. Re-running checks the description as it was when the run started, so
  after changing the description, rely on the run the edit starts.

Automated pull requests follow the rule too. The policyengine package update
(`update-policyengine-package.sh`) opens an issue for each version. The daily
dependency update (`dependency-update-create-pr.sh`) reuses the open "Update
dependencies" issue or opens one. It also reuses an update pull request that is
already open, and replaces its description, `Fixes` line first, because each run
rebuilds the update branch from `main`.

## Changelog Fragments

Rule `sim.process.changelog-fragment`. Every pull request adds at least one
changelog fragment: a file directly in `changelog.d/`, named
`<name>.<type>.md`, that says in a sentence or two what changed for the people
who use or run the simulation API. After the pull request merges, the release
builds every pending fragment into `CHANGELOG.md` with Towncrier and removes
it. Don't edit `CHANGELOG.md` to describe a change.

- **`<name>`** is the tracking issue's number, or another label of letters and
  digits joined by single dots, hyphens or underscores, such as `978` or
  `update-policyengine-6.11.1`. It only keeps fragments apart; the published
  entry doesn't show it.
- **`<type>`** is one of the five types `towncrier.toml` lists. It sets the
  section the entry appears under and the version bump:

  | Type | Section | Raises |
  | --- | --- | --- |
  | `breaking` | Breaking changes | the major version |
  | `added` | Added | the minor version |
  | `removed` | Removed | the minor version |
  | `changed` | Changed | the patch version |
  | `fixed` | Fixed | the patch version |

- **The text** is Markdown, not empty. Several sentences or paragraphs are
  fine.

A pull request that changes several things can add several fragments, of the
same type or of different ones:

```bash
echo "Required a changelog fragment on every pull request." > changelog.d/978.added.md
```

### What the check rejects

The `Changelog fragment` job in `pr.yml` runs
`.github/scripts/check-changelog-fragment.sh` on the merged result. It fails
unless the pull request itself adds a fragment, relative to its base, and
every file in `changelog.d/` other than `.gitkeep` is one. These fail:

- a pull request that adds no fragment, even while `main` has pending
  fragments from other pull requests, and one that only edits a pending
  fragment;
- a file in a folder, such as `changelog.d/added/4083.md`: policyengine-api's
  check accepted that file in #4084, and its release couldn't read it;
- a type `towncrier.toml` doesn't list, such as `978.feature.md` or
  `978.Added.md`;
- another name, such as `978.added`, `978.added.txt`, `978.added.1.md` or
  `README.md`;
- an empty file, or one with only whitespace.

Renaming a misplaced file into place counts as adding it. The definition lives
in `.github/scripts/changelog_fragments.py`, which reads the directory and the
types from `towncrier.toml`, and the release's version bump reads fragments
through the same code. To run the check locally, commit your work and run:

```bash
BASE_SHA=origin/main .github/scripts/check-changelog-fragment.sh
make changelog-draft   # the section the next release would add
```

Both need Python 3.11 or later as `python3`; `make changelog-draft` also needs
uv, to run Towncrier from `.github/towncrier/uv.lock`.

In a stack, the check runs with the rest of CI on the lowest unmerged pull
request only. Each pull request in the stack still adds its own fragment.
Automated pull requests add one too: the daily dependency update adds
`changelog.d/update-dependencies.changed.md`, and the policyengine.py update
adds `changelog.d/update-policyengine-<version>.changed.md`.

### How a merge is released and deployed

Rule `sim.deploy.release-commit-deploys-once`. A push to `main` starts
`.github/workflows/simulation-deploy.yml`:

1. **The merge's push runs only the `versioning` job.** It raises `VERSION`
   (the repository's release version) by the largest bump the pending
   fragments call for, builds their `CHANGELOG.md` section, and pushes one
   commit, `Update PolicyEngine simulation API`, to `main`. It pushes with the
   GitHub App token from `APP_ID` and `APP_PRIVATE_KEY`: the App may push to
   `main` past the branch ruleset, and its push starts the workflow again,
   which a push with the job's own token would not.
2. **The release commit's push runs only the deploy jobs**, beta then prod.
   The `versioning` job is skipped for it, so it releases nothing again and
   nothing loops. API clients publish after this run, not the merge's.

A manual dispatch of the workflow deploys without a release. The workflow's
concurrency group holds each run until the one before it finishes. When two
merges land close together, the later one's run releases both merges'
fragments; if the earlier run's push finds `main` already moved on, it says so
and succeeds without pushing.

If the `versioning` job fails, nothing deploys until a later merge's release
succeeds. It fails, saying which file, when `changelog.d/` holds a file that
isn't a fragment or holds no fragment at all, which only a merge that skipped
the check can cause. Fix it in a pull request, or deploy the current `main`
by dispatching the workflow.

## PR Title

Do not add `[codex]`, `[claude]`, `[copilot]`, or other agent labels to PR
titles. Use a plain descriptive title.
