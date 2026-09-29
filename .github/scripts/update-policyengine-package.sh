#!/usr/bin/env bash
#
# Check PyPI for a newer policyengine.py package, update the simulation project
# requirement, let its models extra select every runtime component, and open one
# bundle-level PR.
#
# Usage:
#   .github/scripts/update-policyengine-package.sh [--dry-run]
#
# Optional environment:
#   PROJECT_DIR      Project containing pyproject.toml and uv.lock.
#   LATEST_OVERRIDE  policyengine version to use instead of querying PyPI (the
#                    repository_dispatch trigger passes the just-released
#                    version here).
#   FORCE=1          Allow targeting a version not newer than the current pin.
#   DRY_RUN=1        Report planned changes without editing files or opening a PR.

set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
elif [[ -n "${1:-}" ]]; then
  echo "ERROR: Unsupported argument '${1}'." >&2
  echo "Usage: update-policyengine-package.sh [--dry-run]" >&2
  exit 1
fi

PACKAGE="policyengine"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(git rev-parse --show-toplevel)"
PROJECT_DIR="${PROJECT_DIR:-projects/policyengine-simulation-executor}"
PROJECT_PATH="${ROOT_DIR}/${PROJECT_DIR}"
PYPROJECT="${PROJECT_PATH}/pyproject.toml"
LOCKFILE="${PROJECT_PATH}/uv.lock"
PYTHON_HELPER="${SCRIPT_DIR}/update_policyengine_package.py"
REPOSITORY="${GITHUB_REPOSITORY:-PolicyEngine/policyengine-sim-api}"
ISSUE_NUMBER=""

ensure_update_issue() {
  local issue_details
  local issue_title
  local issue_url

  issue_title="Update policyengine to ${LATEST}"
  ISSUE_NUMBER=$(
    gh api --paginate --slurp \
      "repos/${REPOSITORY}/issues?state=open&per_page=100" \
      | python3 "$PYTHON_HELPER" find-issue --title "$issue_title"
  )

  if [[ -z "$ISSUE_NUMBER" ]]; then
    issue_url=$(gh issue create \
      --repo "$REPOSITORY" \
      --title "$issue_title" \
      --body "Track the automated simulation runtime update to policyengine ${LATEST}.")
    ISSUE_NUMBER="${issue_url##*/}"
  fi

  if [[ ! "$ISSUE_NUMBER" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Could not resolve an issue for policyengine ${LATEST}." >&2
    exit 1
  fi

  issue_details=$(gh issue view "$ISSUE_NUMBER" \
    --repo "$REPOSITORY" \
    --json number,state,title)
  printf '%s' "$issue_details" \
    | python3 "$PYTHON_HELPER" verify-issue \
      --number "$ISSUE_NUMBER" \
      --title "$issue_title"
}

verify_update_pr() {
  local pr_details

  pr_details=$(gh pr view "$BRANCH" \
    --repo "$REPOSITORY" \
    --json isDraft,headRepositoryOwner,headRepository)
  printf '%s' "$pr_details" \
    | python3 "$PYTHON_HELPER" verify-pr --repository "$REPOSITORY"
}

create_update_pr() {
  local pr_body_file

  ensure_update_issue
  pr_body_file="$(create_pr_body_file)"
  gh pr create \
    --draft \
    --repo "$REPOSITORY" \
    --base main \
    --head "$BRANCH" \
    --title "chore(deps): update policyengine to ${LATEST}" \
    --body-file "$pr_body_file"
  verify_update_pr
}

create_pr_body_file() {
  local pr_body_file

  pr_body_file="$(mktemp)"
  {
    echo "Fixes #${ISSUE_NUMBER}"
    echo
    echo "## Summary"
    echo
    echo "Update policyengine.py from ${CURRENT} to ${LATEST} in the simulation API runtime."
    echo
    echo "The policyengine[models] requirement selects the runtime versions bundled by policyengine.py ${LATEST}:"
    echo "- policyengine-core: ${BUNDLED_CORE_VERSION:-resolved from bundle during update}"
    echo "- policyengine-us: ${BUNDLED_US_VERSION:-resolved from bundle during update}"
    echo "- policyengine-uk: ${BUNDLED_UK_VERSION:-resolved from bundle during update}"
    echo "- spm-calculator: ${BUNDLED_SPM_VERSION:-resolved from bundle during update}"
    echo
    echo "The bundle also selects these certified data releases:"
    echo "- US: ${BUNDLED_US_DATA_VERSION:-resolved from bundle during update}"
    echo "- UK: ${BUNDLED_UK_DATA_VERSION:-resolved from bundle during update}"
    echo
    echo "---"
    echo "Generated automatically by GitHub Actions."
  } > "$pr_body_file"

  echo "$pr_body_file"
}

if [[ ! -f "$PYPROJECT" || ! -f "$LOCKFILE" ]]; then
  echo "ERROR: Expected simulation project files were not found under ${PROJECT_DIR}." >&2
  exit 1
fi

CURRENT=$(python3 "$PYTHON_HELPER" current-version \
  --pyproject "$PYPROJECT" \
  --package "$PACKAGE")

if [[ -n "${LATEST_OVERRIDE:-}" ]]; then
  LATEST="$LATEST_OVERRIDE"
else
  LATEST=$(curl -fsSL "https://pypi.org/pypi/${PACKAGE}/json" \
    | python3 "$PYTHON_HELPER" latest-version --package "$PACKAGE")
  if [[ -z "$LATEST" ]]; then
    echo "ERROR: Could not fetch latest version for ${PACKAGE} from PyPI." >&2
    exit 1
  fi
fi

if [[ -z "$LATEST" ]]; then
  echo "ERROR: Latest version for ${PACKAGE} is empty." >&2
  exit 1
fi

echo "Current pinned version: ${PACKAGE}==${CURRENT}"
echo "Latest PyPI version:   ${PACKAGE}==${LATEST}"

if [[ "$CURRENT" == "$LATEST" ]]; then
  echo "Already up to date. Nothing to do."
  exit 0
fi

if [[ "$(printf '%s\n%s\n' "$CURRENT" "$LATEST" | sort -V | tail -n1)" != "$LATEST" && "${FORCE:-0}" != "1" ]]; then
  echo "Requested ${LATEST} is not newer than current ${CURRENT}. Skipping (set FORCE=1 to override)."
  exit 0
fi

BRANCH="auto/update-policyengine-${LATEST}"
echo "Update available: ${CURRENT} -> ${LATEST}"

if [[ "$DRY_RUN" == "1" ]]; then
  if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    echo "Dry run: remote branch '${BRANCH}' already exists; would ensure a PR exists for it."
    exit 0
  fi
  echo "Dry run: would create ${BRANCH} and update:"
  echo "  ${PROJECT_DIR}/pyproject.toml"
  echo "  ${PROJECT_DIR}/uv.lock"
  exit 0
fi

EXISTING_PR=$(gh pr view "$BRANCH" \
  --repo "$REPOSITORY" \
  --json number,state \
  --jq 'select(.state == "OPEN") | .number' 2>/dev/null || true)
if [[ -n "$EXISTING_PR" ]]; then
  echo "PR #${EXISTING_PR} already exists for ${BRANCH}. Skipping."
  exit 0
fi

if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  echo "Remote branch '${BRANCH}' already exists without an open PR. Creating PR."
  create_update_pr
  echo "PR created for existing branch ${BRANCH}"
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git checkout -b "$BRANCH"

python3 "$PYTHON_HELPER" update-requirements \
  --pyproject "$PYPROJECT" \
  --package "$PACKAGE" \
  --current "$CURRENT" \
  --latest "$LATEST"

# Read the target wrapper's release manifest without resolving the project.
for attempt in 1 2 3; do
  if BUNDLE_OUTPUT=$(
    uv run \
      --isolated \
      --no-project \
      --with "${PACKAGE}==${LATEST}" \
      python "$PYTHON_HELPER" bundle-versions
  ); then
    break
  fi
  if [[ "$attempt" == "3" ]]; then
    echo "ERROR: Could not inspect ${PACKAGE} ${LATEST} after ${attempt} attempts." >&2
    exit 1
  fi
  echo "Bundle inspection attempt ${attempt} failed; retrying in 30s..."
  sleep 30
done

BUNDLED_US_VERSION=$(printf '%s\n' "$BUNDLE_OUTPUT" | awk -F= '$1 == "us_version" {print $2}')
BUNDLED_UK_VERSION=$(printf '%s\n' "$BUNDLE_OUTPUT" | awk -F= '$1 == "uk_version" {print $2}')
BUNDLED_CORE_VERSION=$(printf '%s\n' "$BUNDLE_OUTPUT" | awk -F= '$1 == "policyengine_core_version" {print $2}')
BUNDLED_POLICYENGINE_VERSION=$(printf '%s\n' "$BUNDLE_OUTPUT" | awk -F= '$1 == "policyengine_version" {print $2}')
BUNDLED_SPM_VERSION=$(printf '%s\n' "$BUNDLE_OUTPUT" | awk -F= '$1 == "spm_calculator_version" {print $2}')
BUNDLED_US_DATA_VERSION=$(printf '%s\n' "$BUNDLE_OUTPUT" | awk -F= '$1 == "us_data_version" {print $2}')
BUNDLED_UK_DATA_VERSION=$(printf '%s\n' "$BUNDLE_OUTPUT" | awk -F= '$1 == "uk_data_version" {print $2}')

if [[ -z "$BUNDLED_POLICYENGINE_VERSION" || -z "$BUNDLED_CORE_VERSION" || -z "$BUNDLED_US_VERSION" || -z "$BUNDLED_UK_VERSION" || -z "$BUNDLED_SPM_VERSION" || -z "$BUNDLED_US_DATA_VERSION" || -z "$BUNDLED_UK_DATA_VERSION" ]]; then
  echo "ERROR: Could not resolve bundled runtime package versions." >&2
  echo "$BUNDLE_OUTPUT" >&2
  exit 1
fi
if [[ "$BUNDLED_POLICYENGINE_VERSION" != "$LATEST" ]]; then
  echo "ERROR: Installed policyengine.py reports bundle ${BUNDLED_POLICYENGINE_VERSION}, expected ${LATEST}." >&2
  exit 1
fi

echo "Bundled runtime pins:"
echo "  policyengine==${BUNDLED_POLICYENGINE_VERSION}"
echo "  policyengine-core==${BUNDLED_CORE_VERSION}"
echo "  policyengine-us==${BUNDLED_US_VERSION}"
echo "  policyengine-uk==${BUNDLED_UK_VERSION}"
echo "  spm-calculator==${BUNDLED_SPM_VERSION}"
echo "Certified data releases:"
echo "  us=${BUNDLED_US_DATA_VERSION}"
echo "  uk=${BUNDLED_UK_DATA_VERSION}"

# The PyPI Simple index can briefly lag the JSON API after a release, so retry
# the final project lock as well.
for attempt in 1 2 3; do
  if (
    cd "$PROJECT_PATH"
    uv lock
  ); then
    break
  fi
  if [[ "$attempt" == "3" ]]; then
    echo "ERROR: uv lock failed after ${attempt} attempts." >&2
    exit 1
  fi
  echo "uv lock attempt ${attempt} failed; retrying in 30s..."
  sleep 30
done

(
  cd "$PROJECT_PATH"
  uv lock --check
  uv run --extra test pytest \
    tests/test_bundle_version_export.py \
    tests/test_policyengine_dependency_source.py \
    tests/test_modal_bundle_image.py \
    -q
)

if git diff --quiet -- "$PYPROJECT" "$LOCKFILE"; then
  echo "No changes after update. Nothing to do."
  exit 0
fi

git add "$PYPROJECT" "$LOCKFILE"
git commit -m "chore(deps): update policyengine to ${LATEST}"
git push -u origin "$BRANCH"

create_update_pr

echo "PR created for policyengine ${CURRENT} -> ${LATEST}"
