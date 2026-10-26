#!/usr/bin/env bash

# After an update branch has been pushed, make sure an open dependency-update
# PR exists and that the first line of its description names its tracking
# issue, as rule sim.process.pr-tracking-issue requires of every PR.
#
# The tracking issue is the oldest open issue titled "Update dependencies", or
# a new one when none is open. "Fixes #N" closes it when the PR merges, so the
# next update PR gets a new issue.
#
# - Without an open update PR, open one whose description starts with
#   "Fixes #N".
# - With one, make "Fixes #N" its first line: replace an earlier "Fixes #"
#   line, or add the line above a description that has none (such as a PR
#   opened before the rule).
#
# Environment:
#   GITHUB_REPOSITORY  owner/name of this repository (set by the runner)
#   GH_TOKEN           read by gh: a token that can read and write issues and
#                      pull requests

set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

ISSUE_TITLE="Update dependencies"
ISSUE_BODY="Tracks the automated daily dependency updates from update-dependencies.yml. The update pull request names this issue on the first line of its description and closes it when it merges."
PR_SUMMARY="Automated daily dependency updates. Subsequent runs append commits to this branch while the pull request remains open."

# Set ISSUE_NUMBER to the oldest open "Update dependencies" issue, opening one
# when none is open. The issues API lists pull requests too; they have a
# pull_request field. This sets a variable rather than printing, because bash
# turns off set -e inside $(...), and a failed lookup must not open an issue.
find_or_open_tracking_issue() {
  local open_issues
  local issue_number
  local issue_url

  open_issues="$(
    gh api --paginate \
      "repos/${GITHUB_REPOSITORY}/issues?state=open&per_page=100" \
      --jq ".[] | select(.pull_request == null and .title == \"${ISSUE_TITLE}\") | .number"
  )"
  if [[ -n "${open_issues}" ]]; then
    issue_number="$(sort -n <<<"${open_issues}")"
    issue_number="${issue_number%%$'\n'*}"
  else
    issue_url="$(
      gh issue create \
        --repo "${GITHUB_REPOSITORY}" \
        --title "${ISSUE_TITLE}" \
        --body "${ISSUE_BODY}"
    )"
    issue_number="${issue_url##*/}"
  fi

  if [[ ! "${issue_number}" =~ ^[0-9]+$ ]]; then
    echo "Could not resolve the dependency-update tracking issue: ${issue_number}" >&2
    exit 1
  fi
  ISSUE_NUMBER="${issue_number}"
}

# Print the description with "Fixes #<issue>" as its first line.
description_naming_issue() {
  local description="$1"
  local issue_number="$2"
  local first_line="${description%%$'\n'*}"
  local fixes_line="Fixes #${issue_number}"

  if [[ "${first_line}" =~ ^Fixes\ \#[0-9]+[[:space:]]*$ ]]; then
    if [[ "${description}" == *$'\n'* ]]; then
      printf '%s\n%s' "${fixes_line}" "${description#*$'\n'}"
    else
      printf '%s' "${fixes_line}"
    fi
  else
    printf '%s\n\n%s' "${fixes_line}" "${description}"
  fi
}

open_pr="$(
  gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --base main \
    --head update-dependencies \
    --state open \
    --json number \
    --jq '.[0].number // empty'
)"
ISSUE_NUMBER=""
find_or_open_tracking_issue

if [[ -z "${open_pr}" ]]; then
  gh pr create \
    --repo "${GITHUB_REPOSITORY}" \
    --title "Update dependencies" \
    --body-file - \
    --base main \
    --head update-dependencies \
    <<<"Fixes #${ISSUE_NUMBER}"$'\n\n'"${PR_SUMMARY}"
  exit 0
fi

description="$(
  gh pr view "${open_pr}" \
    --repo "${GITHUB_REPOSITORY}" \
    --json body \
    --jq '.body'
)"
linked_description="$(description_naming_issue "${description}" "${ISSUE_NUMBER}")"
if [[ "${linked_description}" == "${description}" ]]; then
  echo "Pull request #${open_pr} already names tracking issue #${ISSUE_NUMBER}."
  exit 0
fi

gh pr edit "${open_pr}" \
  --repo "${GITHUB_REPOSITORY}" \
  --body-file - \
  <<<"${linked_description}"
echo "Pull request #${open_pr} now names tracking issue #${ISSUE_NUMBER}."
