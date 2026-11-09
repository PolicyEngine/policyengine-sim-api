#!/usr/bin/env bash
# Report a failed weekly comparison of sim's table models with the Public
# API's default branch: comment on the open issue that carries the drift
# label, or open one. Runs with the workflow's own token (GH_TOKEN, with
# issues: write) and GitHub's GITHUB_SERVER_URL, GITHUB_REPOSITORY and
# GITHUB_RUN_ID, which name the failed run.
set -euo pipefail

label="public-api-schema-drift"
run_url="${GITHUB_SERVER_URL:?}/${GITHUB_REPOSITORY:?}/actions/runs/${GITHUB_RUN_ID:?}"

# Creates the label, or leaves it as it is apart from its color and text.
gh label create "${label}" --force --color B60205 \
  --description "The weekly comparison with the Public API's tables failed"

open_issue="$(
  gh issue list --label "${label}" --state open --limit 1 \
    --json number --jq '.[0].number // empty'
)"

if [ -n "${open_issue}" ]; then
  printf 'The comparison failed again: %s\n' "${run_url}" |
    gh issue comment "${open_issue}" --body-file -
  exit 0
fi

gh issue create \
  --title "The weekly comparison with the Public API's tables failed" \
  --label "${label}" \
  --body-file - <<BODY
The \`Public API schema drift\` workflow compares sim's table models with the
tables the Public API's v2 migration chain builds at the head of its
\`master\` branch, and it failed: ${run_url}

Its log says which of these happened:

- The Public API merged a change to a table the simulation service writes,
  or to the service role's grants, that the models in
  \`libs/policyengine-simulation-persistence\` haven't followed. Update the
  models, and move the pinned commit in
  \`libs/policyengine-simulation-persistence/public-api-commit\`, in one pull
  request.
- The Public API's tables couldn't be built at that head.
- A setup step failed, such as the database service, a checkout, or installing
  Python, uv or the dependencies. Then re-run the workflow.

While this issue is open, later failures are added to it as comments. Close
it once the comparison passes again.
BODY
