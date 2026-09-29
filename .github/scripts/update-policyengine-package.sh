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
ROOT_DIR="$(git rev-parse --show-toplevel)"
PROJECT_DIR="${PROJECT_DIR:-projects/policyengine-simulation-executor}"
PROJECT_PATH="${ROOT_DIR}/${PROJECT_DIR}"
PYPROJECT="${PROJECT_PATH}/pyproject.toml"
LOCKFILE="${PROJECT_PATH}/uv.lock"
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
      | python3 -c '
import json
import sys

title = sys.argv[1]
pages = json.load(sys.stdin)
matches = sorted(
    item["number"]
    for page in pages
    for item in page
    if "pull_request" not in item and item.get("title") == title
)
if matches:
    print(matches[0])
' "$issue_title"
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
  printf '%s' "$issue_details" | python3 -c '
import json
import sys

expected_number = int(sys.argv[1])
expected_title = sys.argv[2]
issue = json.load(sys.stdin)
if issue.get("number") != expected_number:
    raise SystemExit("Resolved update issue has an unexpected number")
if issue.get("state") != "OPEN":
    raise SystemExit("Resolved update issue is not open")
if issue.get("title") != expected_title:
    raise SystemExit("Resolved update issue has an unexpected title")
' "$ISSUE_NUMBER" "$issue_title"
}

verify_update_pr() {
  local pr_details

  pr_details=$(gh pr view "$BRANCH" \
    --repo "$REPOSITORY" \
    --json isDraft,headRepositoryOwner,headRepository)
  printf '%s' "$pr_details" | python3 -c '
import json
import sys

expected_repository = sys.argv[1]
pr = json.load(sys.stdin)
if pr.get("isDraft") is not True:
    raise SystemExit("Automated policyengine update PR is not a draft")
head_repository = pr.get("headRepository") or {}
if head_repository.get("nameWithOwner") != expected_repository:
    raise SystemExit("Automated policyengine update PR is not from the canonical repository")
' "$REPOSITORY"
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

CURRENT=$(python3 - "$PYPROJECT" "$PACKAGE" <<'PY'
import sys
import tomllib
from pathlib import Path

pyproject, package = sys.argv[1:]
parsed = tomllib.loads(Path(pyproject).read_text(encoding="utf-8"))
dependencies = parsed.get("project", {}).get("dependencies", [])
prefix = f"{package}[models]=="
matches = [
    dependency.removeprefix(prefix)
    for dependency in dependencies
    if isinstance(dependency, str) and dependency.startswith(prefix)
]
if len(matches) != 1:
    raise SystemExit(
        f"Expected one {package}[models] requirement in project.dependencies; "
        f"found {matches!r}"
    )
print(matches[0])
PY
)

if [[ -n "${LATEST_OVERRIDE:-}" ]]; then
  LATEST="$LATEST_OVERRIDE"
else
  LATEST=$(curl -fsSL "https://pypi.org/pypi/${PACKAGE}/json" | python3 -c 'import json, sys; print(json.load(sys.stdin)["info"]["version"])')
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

python3 - "$PYPROJECT" "$PACKAGE" "$CURRENT" "$LATEST" <<'PY'
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

pyproject_path, package, current, latest = sys.argv[1:]

pyproject = Path(pyproject_path)
pyproject_text = pyproject.read_text(encoding="utf-8")
parsed = tomllib.loads(pyproject_text)
old_requirement = f"{package}[models]=={current}"
new_requirement = f"{package}[models]=={latest}"
requirements = {
    "project.dependencies": parsed.get("project", {}).get("dependencies", []),
    "dependency-groups.modal-simulation-image": parsed.get(
        "dependency-groups", {}
    ).get("modal-simulation-image", []),
}
component_packages = (
    "policyengine-core",
    "policyengine-us",
    "policyengine-uk",
    "spm-calculator",
)


def requirement_name(requirement):
    return re.split(r"[\s\[<>=!~;@]", requirement, maxsplit=1)[0]


for location, dependencies in requirements.items():
    package_prefix = old_requirement.rsplit("==", 1)[0] + "=="
    matches = [
        dependency
        for dependency in dependencies
        if isinstance(dependency, str) and dependency.startswith(package_prefix)
    ]
    if matches != [old_requirement]:
        raise SystemExit(
            f"Expected {old_requirement} in {location}; found {matches!r}"
        )
    redundant_wrapper_requirements = [
        dependency
        for dependency in dependencies
        if isinstance(dependency, str)
        and dependency != old_requirement
        and requirement_name(dependency) == package
    ]
    if redundant_wrapper_requirements:
        raise SystemExit(
            f"Expected only {old_requirement} for {package} in {location}; "
            f"found {redundant_wrapper_requirements!r}"
        )
    direct_component_requirements = [
        dependency
        for dependency in dependencies
        if isinstance(dependency, str)
        and requirement_name(dependency) in component_packages
    ]
    if direct_component_requirements:
        raise SystemExit(
            f"Expected {location} to obtain component packages from "
            f"{package}[models]; found {direct_component_requirements!r}"
        )
old_pin = f'"{old_requirement}"'
new_pin = f'"{new_requirement}"'
if pyproject_text.count(old_pin) != len(requirements):
    raise SystemExit(
        f"Expected {old_pin} {len(requirements)} times in {pyproject}; "
        f"found {pyproject_text.count(old_pin)}"
    )
pyproject_text = pyproject_text.replace(old_pin, new_pin)
with tempfile.NamedTemporaryFile(
    mode="w",
    encoding="utf-8",
    dir=pyproject.parent,
    prefix=f".{pyproject.name}.",
    delete=False,
) as temporary:
    temporary.write(pyproject_text)
    temporary_path = temporary.name
os.replace(temporary_path, pyproject)
PY

# Read the target wrapper's release manifest without resolving the project.
for attempt in 1 2 3; do
  if BUNDLE_OUTPUT=$(
    uv run \
      --isolated \
      --no-project \
      --with "${PACKAGE}==${LATEST}" \
      python - <<'PY'
from policyengine.bundle import get_current_bundle

bundle = get_current_bundle()
packages = bundle.get("packages", {})
data_releases = bundle.get("data_releases", {})


def package_version(name):
    package = packages.get(name, {})
    version = package.get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(f"Bundle has no version for {name}")
    return version


def data_release_version(country):
    release = data_releases.get(country, {})
    data_package = release.get("data_package", {})
    version = release.get("version") or data_package.get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(f"Bundle has no data release version for {country}")
    return version


print(f"policyengine_version={package_version('policyengine')}")
print(f"policyengine_core_version={package_version('policyengine-core')}")
print(f"spm_calculator_version={package_version('spm-calculator')}")
print(f"us_version={package_version('policyengine-us')}")
print(f"us_data_version={data_release_version('us')}")
print(f"uk_version={package_version('policyengine-uk')}")
print(f"uk_data_version={data_release_version('uk')}")
PY
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
