"""Paths, shell and smoke fakes and a GitHub expression reader for deployment tests.

It holds no tests. Its name keeps it among the test files the CI-script guard
searches for script names, and among the deployment test files the rules
registry discovers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
GITIGNORE = REPOSITORY_ROOT / ".gitignore"
TRAFFIC_SCRIPT = (
    REPOSITORY_ROOT
    / ".github"
    / "scripts"
    / "set-cloud-run-simulation-entry-revision.sh"
)
SMOKE_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "cloud-run-simulation-entry-smoke.sh"
)
STAGE12_VALIDATION_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "stage12-validate-infrastructure.sh"
)
CLOUD_RUN_DEPLOY_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "deploy-cloud-run-simulation-entry.sh"
)
DEPLOYMENT_READY_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "simulation-deployment-ready.sh"
)
WORKFLOWS_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
DEPLOY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.yml"
REUSABLE_DEPLOY_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
)
REUSABLE_DEPLOY_WORKFLOW_REFERENCE = (
    "./.github/workflows/simulation-deploy.reusable.yml"
)
# The promotion input every staging deployment passes: true only for a run
# from main, so an accidental manual run from another branch never moves
# staging's stable traffic or deletes its older revisions.
MAIN_ONLY_PROMOTION = "${{ github.ref == 'refs/heads/main' }}"
STAGING_PROMOTION_FIXTURES = (
    REPOSITORY_ROOT
    / "rules"
    / "fixtures"
    / "sim.deploy.staging-promotes-tested-revision"
)
ROLLBACK_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "simulation-rollback.yml"
)
STAGE12_DEPLOY_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "stage12-deploy.yml"
)
PUBLISH_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "publish-clients.yml"
AUTHENTICATED_TESTS = (
    REPOSITORY_ROOT
    / "projects"
    / "policyengine-simulation-entry"
    / "authenticated_tests"
)
DOCKERIGNORE = (
    REPOSITORY_ROOT
    / "projects"
    / "policyengine-simulation-entry"
    / "Dockerfile.dockerignore"
)
DOCKERFILE = (
    REPOSITORY_ROOT / "projects" / "policyengine-simulation-entry" / "Dockerfile"
)


def run_bash(
    *arguments: str | Path,
    check: bool,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run bash on a repository deployment script, with text output."""
    return subprocess.run(
        ["bash", *arguments],
        check=check,
        capture_output=capture_output,
        text=True,
        env=env,
        cwd=cwd,
    )


def write_fake_smoke_curl(tmp_path: Path, revisions: list[str]) -> tuple[Path, Path]:
    state_file = tmp_path / "health-request-count"
    revisions_file = tmp_path / "health-revisions"
    revisions_file.write_text("\n".join(revisions) + "\n", encoding="utf-8")
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env bash
            set -euo pipefail

            headers_file=""
            body_file=""
            url=""
            while [ "$#" -gt 0 ]; do
              case "$1" in
                --dump-header|--output|--request|--header|--data)
                  option="$1"
                  value="$2"
                  shift 2
                  case "${option}" in
                    --dump-header) headers_file="${value}" ;;
                    --output) body_file="${value}" ;;
                  esac
                  ;;
                --fail|--silent|--show-error)
                  shift
                  ;;
                *)
                  url="$1"
                  shift
                  ;;
              esac
            done

            case "${url}" in
              */health)
                count=0
                if [ -f "${FAKE_CURL_STATE}" ]; then
                  count="$(cat "${FAKE_CURL_STATE}")"
                fi
                line_number=$((count + 1))
                revision="$(sed -n "${line_number}p" "${FAKE_CURL_REVISIONS}")"
                printf '%s' "${line_number}" > "${FAKE_CURL_STATE}"
                printf 'HTTP/1.1 200 OK\r\nX-PolicyEngine-Simulation-Revision: %s\r\n\r\n' \
                  "${revision}" > "${headers_file}"
                printf '{"status":"healthy"}' > "${body_file}"
                ;;
              */ready)
                printf '{"status":"ready"}'
                ;;
              */versions)
                versions='{}'
                if [ -n "${FAKE_CURL_VERSIONS:-}" ]; then
                  versions="$(cat "${FAKE_CURL_VERSIONS}")"
                fi
                if [ -n "${body_file}" ]; then
                  printf '%s' "${versions}" > "${body_file}"
                else
                  printf '%s' "${versions}"
                fi
                ;;
              */ping)
                printf '{"incremented":2}'
                ;;
              *)
                printf 'Unexpected fake curl URL: %s\n' "${url}" >&2
                exit 2
                ;;
            esac
            """),  # noqa: E501  # the fake curl writes its HTTP header block with one printf
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    return state_file, revisions_file


def smoke_test_env(
    tmp_path: Path,
    state_file: Path,
    revisions_file: Path,
) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CURL_STATE": str(state_file),
        "FAKE_CURL_REVISIONS": str(revisions_file),
        "SIMULATION_ENTRYPOINT_ROUTING_MAX_ATTEMPTS": "3",
        "SIMULATION_ENTRYPOINT_ROUTING_RETRY_DELAY_SECONDS": "0",
    }


def job_section(workflow: str, job_name: str) -> str:
    match = re.search(
        rf"^  {job_name}:\n(?P<body>.*?)(?=^  [a-z0-9_-]+:\n|\Z)",
        workflow,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, job_name
    return match.group("body")


def sentinel_check_step(job: dict) -> dict:
    steps = [
        step
        for step in job["steps"]
        if "src.modal.utils.release_sentinel check" in step.get("run", "")
    ]
    assert len(steps) == 1
    return steps[0]


# A small evaluator for the GitHub Actions expression subset these workflows
# use (&&, ||, !, ==, !=, parentheses, status functions, property access and
# string literals), so conditions are tested by what they decide, not by
# their text. It follows GitHub's rules where they matter here: a job ``if``
# with no status function is implicitly ``success() && (...)``; ``==``
# compares strings case-insensitively and converts mismatched types to
# numbers (true is 1, null is 0, a non-numeric string is NaN, so
# ``true == 'true'`` is false).
_TOKEN = re.compile(r"\s*(\$\{\{|\}\}|&&|\|\||==|!=|!|\(|\)|'[^']*'|[A-Za-z_][\w.-]*)")
_STATUS_FUNCTIONS = ("always", "success", "failure", "cancelled")


def _gh_number(value):
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _gh_equal(left, right) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    if type(left) is type(right):
        return left == right
    return _gh_number(left) == _gh_number(right)


def _gh_tokens(expression: str) -> list[str]:
    """Split an expression into tokens, dropping its ``${{`` and ``}}``."""
    tokens = []
    position = 0
    text = expression.strip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        assert match, text[position:]
        tokens.append(match.group(1))
        position = match.end()
    return [token for token in tokens if token not in ("${{", "}}")]


def _gh_calls_a_status_function(tokens: list[str]) -> bool:
    return any(
        token in _STATUS_FUNCTIONS and tokens[index + 1 : index + 2] == ["("]
        for index, token in enumerate(tokens)
    )


def _gh_truthy(value) -> bool:
    return value not in (None, False, "", 0)


class _GitHubExpression:
    """A recursive-descent evaluation of one tokenized expression in a context.

    Each ``_parse_*`` method reads from a token index and returns the value
    it read and the index after it, from the loosest operator (``||``) to the
    tightest (``!``, parentheses and operands).
    """

    def __init__(self, tokens: list[str], context: dict) -> None:
        self.tokens = tokens
        self.context = context
        self.results = [
            job.get("result") for job in (context.get("needs") or {}).values()
        ]

    def evaluate(self):
        value, index = self._parse_or(0)
        assert index == len(self.tokens), self.tokens[index:]
        return value

    def _next_is(self, index: int, operators: tuple[str, ...]) -> bool:
        return index < len(self.tokens) and self.tokens[index] in operators

    def _status(self, name: str) -> bool:
        cancelled = bool(self.context.get("cancelled"))
        if name == "always":
            return True
        if name == "cancelled":
            return cancelled
        if name == "failure":
            return "failure" in self.results
        return not cancelled and all(result == "success" for result in self.results)

    def _lookup(self, path: str):
        if path in ("true", "false"):
            return path == "true"
        value = self.context
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def _parse_or(self, index: int):
        left, index = self._parse_and(index)
        while self._next_is(index, ("||",)):
            right, index = self._parse_and(index + 1)
            left = left if _gh_truthy(left) else right
        return left, index

    def _parse_and(self, index: int):
        left, index = self._parse_compare(index)
        while self._next_is(index, ("&&",)):
            right, index = self._parse_compare(index + 1)
            left = right if _gh_truthy(left) else left
        return left, index

    def _parse_compare(self, index: int):
        left, index = self._parse_unary(index)
        while self._next_is(index, ("==", "!=")):
            operator = self.tokens[index]
            right, index = self._parse_unary(index + 1)
            equal = _gh_equal(left, right)
            left = equal if operator == "==" else not equal
        return left, index

    def _parse_unary(self, index: int):
        symbol = self.tokens[index]
        if symbol == "!":
            value, index = self._parse_unary(index + 1)
            return not _gh_truthy(value), index
        if symbol == "(":
            value, index = self._parse_or(index + 1)
            assert self.tokens[index] == ")"
            return value, index + 1
        if symbol.startswith("'"):
            return symbol[1:-1], index + 1
        if self.tokens[index + 1 : index + 3] == ["(", ")"]:
            assert symbol in _STATUS_FUNCTIONS, symbol
            return self._status(symbol), index + 3
        return self._lookup(symbol), index + 1


def evaluate_github_expression(expression: str, context: dict, *, job_if: bool = False):
    tokens = _gh_tokens(expression)
    if job_if and not _gh_calls_a_status_function(tokens):
        tokens = ["success", "(", ")", "&&", "(", *tokens, ")"]
    return _GitHubExpression(tokens, context).evaluate()


def routing_context(**overrides):
    context = {
        "inputs": {"deploy_existing_stack": True, "promote_entrypoint": True},
        "github": {"run_id": "744001", "run_attempt": "1"},
        "needs": {
            "update_routing": {
                "result": "success",
                "outputs": {
                    "before_sha256": "a" * 64,
                    "after_sha256": "b" * 64,
                    "advances_latest": "true",
                },
            },
            "integration": {"result": "success"},
            "authenticated_test": {"result": "success"},
            "promote_entrypoint": {"result": "success"},
        },
    }
    for path, value in overrides.items():
        target = context
        *parents, last = path.split("__")
        for part in parents:
            target = target[part]
        target[last] = value
    return context


# The gateway's real /versions response for two published releases (6.2.7
# latest); test_versions_fixture.py in the gateway project keeps it current.
VERSIONS_BODY = json.loads(
    (Path(__file__).parent / "fixtures" / "versions_body.json").read_text()
)


def run_identity_smoke(tmp_path: Path, versions: dict, **expected: str):
    import json

    revision = "policyengine-simulation-entry-00002-new"
    state_file, revisions_file = write_fake_smoke_curl(tmp_path, [revision])
    versions_file = tmp_path / "versions.json"
    versions_file.write_text(json.dumps(versions), encoding="utf-8")
    env = {
        **smoke_test_env(tmp_path, state_file, revisions_file),
        "FAKE_CURL_VERSIONS": str(versions_file),
        **expected,
    }
    return run_bash(
        SMOKE_SCRIPT,
        "https://simulation.example.test",
        revision,
        check=False,
        capture_output=True,
        env=env,
    )


IDENTITY = {
    "EXPECTED_IDENTITY": "required",
    "EXPECTED_POLICYENGINE_VERSION": "6.2.7",
    "EXPECTED_APP_NAME": "policyengine-simulation-py6-2-7",
    "EXPECTED_UK_MODEL_VERSION": "2.126.8",
    "EXPECTED_UK_DATASET_SHA256": "a" * 64,
    "EXPECTED_LATEST": "true",
}


def workflow_jobs(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]
