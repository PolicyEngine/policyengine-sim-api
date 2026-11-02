#!/usr/bin/env bash

# Commit the release build-changelog.sh prepared, and push it to main.
#
# The release commit holds only the raised VERSION, the rebuilt CHANGELOG.md
# and the removal of the fragments Towncrier built it from; any other change
# to a tracked file fails the script. Its whole message is
# RELEASE_COMMIT_MESSAGE. simulation-deploy.yml deploys a push to main whose
# head commit has that message and releases every other push, so this push
# deploys, and releases nothing again (rule
# sim.deploy.release-commit-deploys-once).
#
# git pushes with the GitHub App token the checkout stored. The App is on the
# main branch ruleset's bypass list, so it may push without a pull request.
#
# When main moved on to a newer merge while this release was built, the push
# is refused. That merge's run waits for this one in the deployment
# concurrency group, and its release builds these fragments too, so the script
# says so and exits 0. Any other refusal fails it.
#
# Environment:
#   RELEASE_COMMIT_MESSAGE  the release commit's message

set -euo pipefail

message="${RELEASE_COMMIT_MESSAGE:?RELEASE_COMMIT_MESSAGE is required}"
release_paths=(VERSION CHANGELOG.md changelog.d)

merge_commit="$(git rev-parse HEAD)"
git add --all -- "${release_paths[@]}"

outside_release="$(git status --porcelain --untracked-files=no -- . "${release_paths[@]/#/:!}")"
if [[ -n "${outside_release}" ]]; then
  echo "::error::The release build changed files outside VERSION, CHANGELOG.md and changelog.d/; nothing was pushed:"
  echo "${outside_release}"
  exit 1
fi
if git diff --cached --quiet; then
  echo "::error::Nothing to release: build-changelog.sh changed none of VERSION, CHANGELOG.md and changelog.d/."
  exit 1
fi

# The identity variables take precedence over any git configuration.
bot_name="github-actions[bot]"
bot_email="github-actions[bot]@users.noreply.github.com"
GIT_AUTHOR_NAME="${bot_name}" GIT_AUTHOR_EMAIL="${bot_email}" \
  GIT_COMMITTER_NAME="${bot_name}" GIT_COMMITTER_EMAIL="${bot_email}" \
  git commit --quiet --message "${message}"
release_commit="$(git rev-parse HEAD)"

if git push origin "HEAD:refs/heads/main"; then
  echo "Pushed release commit ${release_commit} to main."
  exit 0
fi

git fetch --quiet origin main
remote_main="$(git rev-parse FETCH_HEAD)"
if [[ "${remote_main}" != "${merge_commit}" ]] \
  && git merge-base --is-ancestor "${merge_commit}" "${remote_main}"; then
  echo "::notice::main moved on to ${remote_main} while this release was built. The run for that push releases these fragments with its own."
  exit 0
fi
echo "::error::Could not push the release commit to main, and main (${remote_main}) does not hold the merge ${merge_commit} this run released."
exit 1
