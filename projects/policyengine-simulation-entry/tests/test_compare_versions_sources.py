"""Tests for .github/scripts/compare_versions_sources.py.

The staging deploy runs the script against its candidate revision to compare
the old gateway's ``/versions`` answers, which the entrypoint forwards, with
the v2 manifest's answers on ``/internal/v2/versions``. Tests of the
comparison load the script in this process; tests of a whole run start it as
the workflow does, against a local HTTP server, so no test leaves this
machine.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest
import yaml
from workflow_script_support import SCRIPTS_DIR, WORKFLOWS_DIR

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

COMPARE_SCRIPT = SCRIPTS_DIR / "compare_versions_sources.py"
REUSABLE_DEPLOY_WORKFLOW = WORKFLOWS_DIR / "simulation-deploy.reusable.yml"
COMPARE_STEP = "Compare /versions with the v2 manifest's answers (report only)"
# The gateway's real /versions answer for two published releases; the
# gateway project's test_versions_fixture.py keeps it current.
GATEWAY_ANSWER = json.loads(
    (Path(__file__).parent / "fixtures" / "versions_body.json").read_text()
)
UNKNOWN_KIND_BODY = {"detail": "Unknown version kind: unknown-kind"}


def manifest_answer_for(gateway_answer: dict) -> dict:
    """Return ``gateway_answer`` as the manifest gives it: v2 application names."""
    answer = copy.deepcopy(gateway_answer)
    for kind in ("policyengine", "us", "uk"):
        answer[kind] = {
            key: value
            if key == "latest"
            else value.replace(
                "policyengine-simulation-py", "policyengine-simulation-v2-py"
            )
            for key, value in answer[kind].items()
        }
    return answer


def route_answers(gateway_answer: dict, manifest_answer: dict) -> dict:
    """Return each route's (status, body) for two whole answers."""
    routes = {
        "/versions": (200, gateway_answer),
        "/internal/v2/versions": (200, manifest_answer),
        "/versions/unknown-kind": (404, UNKNOWN_KIND_BODY),
        "/internal/v2/versions/unknown-kind": (404, UNKNOWN_KIND_BODY),
    }
    for kind in ("policyengine", "us", "uk"):
        routes[f"/versions/{kind}"] = (200, gateway_answer[kind])
        routes[f"/internal/v2/versions/{kind}"] = (200, manifest_answer[kind])
    return routes


@pytest.fixture(scope="module")
def compare_module() -> ModuleType:
    """Load the comparison script as a module in this process."""
    spec = importlib.util.spec_from_file_location(
        "compare_versions_sources", COMPARE_SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def differences_between(compare_module, routes: dict) -> list:
    def fetch_answer(url: str):
        path = url.removeprefix("https://candidate.example")
        status, body = routes[path]
        return compare_module.Answer(status=status, body=body)

    return compare_module.compare_sources("https://candidate.example", fetch_answer)


def test_answers_that_differ_only_in_application_names_match(compare_module):
    routes = route_answers(GATEWAY_ANSWER, manifest_answer_for(GATEWAY_ANSWER))

    assert differences_between(compare_module, routes) == []


def test_a_version_only_the_gateway_routes_is_a_difference(compare_module):
    manifest_answer = manifest_answer_for(GATEWAY_ANSWER)
    del manifest_answer["policyengine"]["6.2.6"]
    del manifest_answer["uk"]["2.125.1"]
    del manifest_answer["bundles"]["6.2.6"]

    differences = differences_between(
        compare_module, route_answers(GATEWAY_ANSWER, manifest_answer)
    )

    found = {(difference.where, difference.key) for difference in differences}
    assert ("GET /versions: policyengine", "6.2.6") in found
    assert ("GET /versions: uk", "2.125.1") in found
    assert ("GET /versions/uk", "2.125.1") in found
    assert ("GET /versions: bundles", "6.2.6") in found
    [policyengine_difference] = [
        difference
        for difference in differences
        if difference.where == "GET /versions: policyengine"
    ]
    assert policyengine_difference.gateway == "serves policyengine 6.2.6"
    assert policyengine_difference.manifest is compare_module.ABSENT


def test_a_model_version_routed_to_another_bundle_is_a_difference(compare_module):
    manifest_answer = manifest_answer_for(GATEWAY_ANSWER)
    manifest_answer["us"]["1.700.0"] = "policyengine-simulation-v2-py6-2-6"

    differences = differences_between(
        compare_module, route_answers(GATEWAY_ANSWER, manifest_answer)
    )

    assert [
        (difference.where, difference.key, difference.gateway, difference.manifest)
        for difference in differences
    ] == [
        (
            "GET /versions: us",
            "1.700.0",
            "serves policyengine 6.2.7",
            "serves policyengine 6.2.6",
        ),
        (
            "GET /versions/us",
            "1.700.0",
            "serves policyengine 6.2.7",
            "serves policyengine 6.2.6",
        ),
    ]


def test_a_different_latest_version_is_a_difference(compare_module):
    manifest_answer = manifest_answer_for(GATEWAY_ANSWER)
    manifest_answer["policyengine"]["latest"] = "6.2.6"

    differences = differences_between(
        compare_module, route_answers(GATEWAY_ANSWER, manifest_answer)
    )

    assert {
        (difference.where, difference.key, difference.gateway, difference.manifest)
        for difference in differences
    } == {
        ("GET /versions: policyengine", "latest", "6.2.7", "6.2.6"),
        ("GET /versions/policyengine", "latest", "6.2.7", "6.2.6"),
    }


def test_bundle_fields_and_spm_capabilities_are_compared_value_by_value(
    compare_module,
):
    gateway_answer = copy.deepcopy(GATEWAY_ANSWER)
    capability = {"contract_version": "canonical-spm-v1", "defaults": {}}
    gateway_answer["spm_capabilities"] = {"6.2.7": capability}
    manifest_answer = manifest_answer_for(GATEWAY_ANSWER)
    manifest_answer["bundles"]["6.2.6"]["us"]["dataset_sha256"] = "e" * 64

    differences = differences_between(
        compare_module, route_answers(gateway_answer, manifest_answer)
    )

    assert {
        (difference.where, difference.key, json.dumps(difference.gateway, default=repr))
        for difference in differences
    } == {
        ("GET /versions: bundles", "6.2.6.us.dataset_sha256", "null"),
        (
            "GET /versions: spm_capabilities",
            "6.2.7",
            json.dumps(capability),
        ),
    }


def test_a_route_that_does_not_answer_is_reported_by_status(compare_module):
    routes = route_answers(GATEWAY_ANSWER, manifest_answer_for(GATEWAY_ANSWER))
    routes["/internal/v2/versions"] = (
        503,
        {"detail": "The v2 version manifest is unavailable."},
    )

    differences = differences_between(compare_module, routes)

    assert [
        (difference.where, difference.key, difference.gateway, difference.manifest)
        for difference in differences
    ] == [("GET /versions", "status", "HTTP 200", "HTTP 503")]


def test_an_unknown_kind_must_be_rejected_the_same_way(compare_module):
    routes = route_answers(GATEWAY_ANSWER, manifest_answer_for(GATEWAY_ANSWER))
    routes["/internal/v2/versions/unknown-kind"] = (404, {"detail": "Not Found"})

    differences = differences_between(compare_module, routes)

    assert [(difference.where, difference.key) for difference in differences] == [
        ("GET /versions/unknown-kind", "body.detail")
    ]


def test_the_report_lists_each_difference(compare_module):
    report = compare_module.render_report(
        "https://candidate.example",
        [
            compare_module.Difference(
                "GET /versions: policyengine",
                "4.18.3",
                "serves policyengine 4.18.3",
                compare_module.ABSENT,
            )
        ],
    )

    assert "Compared at `https://candidate.example`." in report
    assert "does not fail the deploy" in report
    assert "**1 difference.**" in report
    assert (
        "| GET /versions: policyengine | 4.18.3 | serves policyengine 4.18.3 "
        "| (absent) |"
    ) in report


class _CannedRoutes(BaseHTTPRequestHandler):
    routes: ClassVar[dict] = {}

    def do_GET(self):
        status, body = self.routes.get(self.path, (404, {"detail": "Not Found"}))
        content = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        return


@contextmanager
def serving(routes: dict) -> Iterator[str]:
    """Serve ``routes`` on a loopback port and yield the base URL."""
    handler = type("CannedRoutes", (_CannedRoutes,), {"routes": routes})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def run_comparison(*args: str, summary: Path) -> subprocess.CompletedProcess[str]:
    """Run the script as the workflow step does."""
    return subprocess.run(
        [sys.executable, str(COMPARE_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env={"GITHUB_STEP_SUMMARY": str(summary), "PATH": "/usr/bin:/bin"},
        timeout=60,
    )


def test_a_run_with_matching_answers_reports_a_match(tmp_path):
    summary = tmp_path / "summary.md"
    routes = route_answers(GATEWAY_ANSWER, manifest_answer_for(GATEWAY_ANSWER))

    with serving(routes) as base_url:
        result = run_comparison(f"{base_url}/", summary=summary)

    assert result.returncode == 0, result.stderr
    assert "**The answers match.**" in result.stdout
    assert summary.read_text(encoding="utf-8") in result.stdout
    assert f"Compared at `{base_url}`." in summary.read_text(encoding="utf-8")


def test_a_run_with_differences_reports_them_and_still_succeeds(tmp_path):
    summary = tmp_path / "summary.md"
    manifest_answer = manifest_answer_for(GATEWAY_ANSWER)
    manifest_answer["spm_capabilities"] = {"6.2.7": {"contract_version": "x"}}

    with serving(route_answers(GATEWAY_ANSWER, manifest_answer)) as base_url:
        result = run_comparison(base_url, summary=summary)

    assert result.returncode == 0, result.stderr
    assert "**1 difference.**" in summary.read_text(encoding="utf-8")


def test_a_run_where_no_route_answers_reports_it_and_still_succeeds(tmp_path):
    summary = tmp_path / "summary.md"
    with serving({}) as base_url:
        unreachable_url = base_url
    # The server has stopped, so every request is refused.

    result = run_comparison(unreachable_url, summary=summary)

    assert result.returncode == 0, result.stderr
    report = summary.read_text(encoding="utf-8")
    assert "| GET /versions | status | no answer (" in report
    assert "**5 differences.**" in report


@pytest.mark.parametrize("args", [(), ("",), ("https://a.example", "extra")])
def test_a_run_without_exactly_one_base_url_is_a_usage_error(tmp_path, args):
    summary = tmp_path / "summary.md"

    result = run_comparison(*args, summary=summary)

    assert result.returncode == 2
    assert "Usage: compare_versions_sources.py BASE_URL" in result.stderr
    assert not summary.exists()


def _integration_steps() -> list[dict]:
    workflow = yaml.safe_load(REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["integration"]["steps"]


def test_the_staging_deploy_compares_the_candidates_two_answers():
    steps = _integration_steps()
    names = [step.get("name") for step in steps]
    [compare_step] = [step for step in steps if step.get("name") == COMPARE_STEP]

    # After the candidate's own smoke check, before the numerical checks.
    assert names.index(COMPARE_STEP) == (
        names.index("Verify the complete deployed request path") + 1
    )
    assert compare_step["if"] == "${{ inputs.deployment_environment == 'staging' }}"
    assert compare_step["env"] == {
        "CANDIDATE_URL": "${{ needs.deploy_entrypoint.outputs.candidate_url }}"
    }
    assert compare_step["run"] == (
        'python .github/scripts/compare_versions_sources.py "${CANDIDATE_URL}"'
    )
    # A report-only step: nothing may turn its result into a pass.
    assert "continue-on-error" not in compare_step
