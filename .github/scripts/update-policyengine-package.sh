#!/usr/bin/env bash
#
# Check PyPI for a newer policyengine.py package, update the simulation project
# pin, sync runtime package pins to that policyengine.py bundle, and open one
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

create_pr_body_file() {
  local pr_body_file

  pr_body_file="$(mktemp)"
  {
    echo "## Summary"
    echo
    echo "Update policyengine.py from ${CURRENT} to ${LATEST} in the simulation API runtime."
    echo
    echo "This also syncs runtime package pins to the versions bundled by policyengine.py ${LATEST}:"
    echo "- policyengine-core: ${BUNDLED_CORE_VERSION:-resolved from bundle during update}"
    echo "- policyengine-us: ${BUNDLED_US_VERSION:-resolved from bundle during update}"
    echo "- policyengine-uk: ${BUNDLED_UK_VERSION:-resolved from bundle during update}"
    echo
    echo "Country data package versions remain manifest-derived at runtime/deploy time rather than independently pinned here."
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
import re
import sys
from pathlib import Path

pyproject, package = sys.argv[1:]
text = Path(pyproject).read_text(encoding="utf-8")
match = re.search(rf'"{re.escape(package)}==([^"]+)"', text)
if not match:
    raise SystemExit(f"Package {package!r} not found in {pyproject}")
print(match.group(1))
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

EXISTING_PR=$(gh pr list \
  --head "$BRANCH" \
  --state open \
  --json number \
  --jq '.[0].number' 2>/dev/null || true)
if [[ -n "$EXISTING_PR" ]]; then
  echo "PR #${EXISTING_PR} already exists for ${BRANCH}. Skipping."
  exit 0
fi

if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  echo "Remote branch '${BRANCH}' already exists without an open PR. Creating PR."
  PR_BODY_FILE="$(create_pr_body_file)"
  gh pr create \
    --base main \
    --head "$BRANCH" \
    --title "chore(deps): update policyengine to ${LATEST}" \
    --body-file "$PR_BODY_FILE"
  echo "PR created for existing branch ${BRANCH}"
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git checkout -b "$BRANCH"

python3 - "$PYPROJECT" "$PACKAGE" "$CURRENT" "$LATEST" <<'PY'
import sys
from pathlib import Path

pyproject_path, package, current, latest = sys.argv[1:]

pyproject = Path(pyproject_path)
pyproject_text = pyproject.read_text(encoding="utf-8")
old_pin = f'"{package}=={current}"'
new_pin = f'"{package}=={latest}"'
if old_pin not in pyproject_text:
    raise SystemExit(f"Could not find {old_pin} in {pyproject}")
pyproject.write_text(pyproject_text.replace(old_pin, new_pin), encoding="utf-8")
PY

# The PyPI Simple index (which uv resolves from) can lag the JSON API right
# after a release, so retry the lock a few times.
for attempt in 1 2 3; do
  if (
    cd "$PROJECT_PATH"
    uv lock --upgrade-package "$PACKAGE"
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

BUNDLE_OUTPUT=$(
  cd "$PROJECT_PATH"
  uv run python -m src.modal.utils.extract_bundle_versions --shell
)
BUNDLED_US_VERSION=$(printf '%s\n' "$BUNDLE_OUTPUT" | awk -F= '$1 == "us_version" {print $2}')
BUNDLED_UK_VERSION=$(printf '%s\n' "$BUNDLE_OUTPUT" | awk -F= '$1 == "uk_version" {print $2}')
BUNDLED_CORE_VERSION=$(printf '%s\n' "$BUNDLE_OUTPUT" | awk -F= '$1 == "policyengine_core_version" {print $2}')

if [[ -z "$BUNDLED_CORE_VERSION" || -z "$BUNDLED_US_VERSION" || -z "$BUNDLED_UK_VERSION" ]]; then
  echo "ERROR: Could not resolve bundled runtime package versions." >&2
  echo "$BUNDLE_OUTPUT" >&2
  exit 1
fi

echo "Bundled runtime pins:"
echo "  policyengine-core==${BUNDLED_CORE_VERSION}"
echo "  policyengine-us==${BUNDLED_US_VERSION}"
echo "  policyengine-uk==${BUNDLED_UK_VERSION}"

python3 - "$PYPROJECT" "$BUNDLED_CORE_VERSION" "$BUNDLED_US_VERSION" "$BUNDLED_UK_VERSION" <<'PY'
import re
import sys
from pathlib import Path

pyproject_path, core_version, us_version, uk_version = sys.argv[1:]
pyproject = Path(pyproject_path)
text = pyproject.read_text(encoding="utf-8")
pins = {
    "policyengine-core": core_version,
    "policyengine-us": us_version,
    "policyengine-uk": uk_version,
}
for package, version in pins.items():
    pattern = rf'"{re.escape(package)}==[^"]+"'
    replacement = f'"{package}=={version}"'
    text, count = re.subn(pattern, replacement, text, count=1)
    if count != 1:
        raise SystemExit(f"Could not update {package} in {pyproject}")
pyproject.write_text(text, encoding="utf-8")
PY

(
  cd "$PROJECT_PATH"
  uv lock
)

if git diff --quiet -- "$PYPROJECT" "$LOCKFILE"; then
  echo "No changes after update. Nothing to do."
  exit 0
fi

PR_BODY_FILE="$(create_pr_body_file)"

git add "$PYPROJECT" "$LOCKFILE"
git commit -m "chore(deps): update policyengine to ${LATEST}"
git push -u origin "$BRANCH"

gh pr create \
  --base main \
  --title "chore(deps): update policyengine to ${LATEST}" \
  --body-file "$PR_BODY_FILE"

echo "PR created for policyengine ${CURRENT} -> ${LATEST}"
