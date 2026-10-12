"""Workflow concurrency is the routing publisher's only serialization primitive."""

from __future__ import annotations

import ast
import itertools
import re
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
REGISTRY_MODULE = (
    REPOSITORY_ROOT
    / "projects/policyengine-simulation-executor/src/modal/utils/update_version_registry.py"
)
REGISTRY_COMMAND = "src.modal.utils.update_version_registry"
REGISTRY_INVOCATION = re.compile(
    r"(?:src\.modal\.utils\.update_version_registry|"
    r"(?:[\w./-]*/)?update_version_registry\.py)\b"
)


def _workflows() -> dict[str, dict]:
    return {
        path.relative_to(REPOSITORY_ROOT).as_posix(): yaml.safe_load(path.read_text())
        for path in sorted(WORKFLOW_DIRECTORY.iterdir())
        if path.suffix in {".yml", ".yaml"}
    }


def _routing_commands(script: str):
    """Inspect actual commands, resolving the shell argument arrays we use.

    Unknown/default modes count as writers. A plan or identity command cannot
    hide another writing invocation in the same step.
    """

    logical_lines = re.sub(r"\\\n\s*", " ", script).splitlines()
    for line in logical_lines:
        if line.lstrip().startswith("#"):
            continue
        invocations = list(REGISTRY_INVOCATION.finditer(line))
        for index, invocation in enumerate(invocations):
            end = (
                invocations[index + 1].start()
                if index + 1 < len(invocations)
                else len(line)
            )
            arguments = line[invocation.end() : end]
            for array in re.findall(r"\$\{(\w+)\[@\]\}", arguments):
                assignments = re.findall(
                    rf"\b{re.escape(array)}\+?=\((.*?)\)", script, re.DOTALL
                )
                assert assignments, f"Cannot resolve routing argument array {array}"
                arguments += " " + " ".join(assignments)
            yield arguments


def _writing_steps(job: dict):
    for step in job.get("steps", []):
        for arguments in _routing_commands(step.get("run", "")):
            writes = any(
                flag in arguments
                for flag in (
                    "--apply-plan",
                    "--restore-plan",
                    "--restore-latest",
                    "--seed-active-from-legacy",
                )
            )
            reads = any(
                flag in arguments
                for flag in (
                    "--plan-out",
                    "--dry-run",
                    "--print-identity",
                    "--print-latest",
                )
            )
            if writes or not reads:
                yield step


def _called_workflow(job: dict, workflows: dict[str, dict]) -> str | None:
    uses = job.get("uses", "")
    canonical_prefix = "PolicyEngine/policyengine-sim-api/"
    if uses.startswith("./.github/workflows/"):
        path = uses.removeprefix("./")
    elif uses.lower().startswith(canonical_prefix.lower()):
        # Same-repository qualified calls must obey the same writer invariant
        # as relative calls, including when pinned to a ref or commit.
        path, separator, reference = uses[len(canonical_prefix) :].partition("@")
        assert separator and reference, f"Missing reusable workflow ref: {uses}"
        assert path.startswith(".github/workflows/"), uses
    else:
        return None
    assert path in workflows, f"Missing local reusable workflow {path}"
    return path


def _writer_jobs(path: str, workflows: dict[str, dict], ancestors=()) -> set[str]:
    assert path not in ancestors, f"Recursive reusable workflow: {ancestors}, {path}"
    result = set()
    for job_id, job in workflows[path]["jobs"].items():
        called = _called_workflow(job, workflows)
        if list(_writing_steps(job)) or (
            called and _writer_jobs(called, workflows, (*ancestors, path))
        ):
            result.add(job_id)
    return result


def _runnable_writer_workflows(workflows: dict[str, dict]) -> set[str]:
    writers = set()
    for path, workflow in workflows.items():
        # PyYAML's YAML 1.1 loader parses the unquoted `on` key as True.
        triggers = workflow.get("on", workflow.get(True))
        only_reusable = isinstance(triggers, dict) and set(triggers) == {
            "workflow_call"
        }
        if not only_reusable and _writer_jobs(path, workflows):
            writers.add(path)
    return writers


def _guard_allowed_workflows() -> set[str]:
    # Read the literal source of truth without importing another service's
    # runtime dependencies into entrypoint asset tests.
    module = ast.parse(REGISTRY_MODULE.read_text())
    assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "SERIALIZED_ROUTING_WORKFLOW_FILES"
            for target in node.targets
        )
    )
    value = assignment.value
    if isinstance(value, ast.Call):
        assert isinstance(value.func, ast.Name) and value.func.id == "frozenset"
        value = value.args[0]
    return set(ast.literal_eval(value))


def test_every_runnable_routing_writer_declares_repository_serialization():
    workflows = _workflows()
    writers = _runnable_writer_workflows(workflows)
    assert writers, "No routing writer workflows discovered"
    for path in sorted(writers):
        assert workflows[path].get("concurrency") == {
            "group": "simulation-deploy-main",
            "cancel-in-progress": False,
        }, f"{path} must serialize routing writes across workflows"


def test_routing_guard_allows_exactly_the_discovered_writer_workflows():
    workflows = _workflows()
    writers = _runnable_writer_workflows(workflows)
    assert _guard_allowed_workflows() == writers
    serialized = {
        path
        for path, workflow in workflows.items()
        if isinstance(workflow.get("concurrency"), dict)
        and workflow["concurrency"].get("group") == "simulation-deploy-main"
    }
    assert serialized == writers


def _resolve_environment(value: str, inputs: dict) -> frozenset[str]:
    if "${{" not in value:
        return frozenset({value})
    direct_input = re.fullmatch(r"\$\{\{\s*inputs\.(\w+)\s*\}\}", value)
    if direct_input:
        return _resolve_environment(inputs[direct_input.group(1)], inputs)
    # A conditional environment can target either branch. Treat both as live
    # so a new same-environment parallel writer cannot bypass the check.
    conditional = re.fullmatch(
        r"\$\{\{.*?&&\s*'([^']+)'\s*\|\|\s*'([^']+)'\s*\}\}", value
    )
    assert conditional, f"Cannot prove routing environment for {value}"
    return frozenset(conditional.groups())


def _writer_environment(workflow: dict, job: dict, step: dict, inputs: dict):
    environment = {
        **workflow.get("env", {}),
        **job.get("env", {}),
        **step.get("env", {}),
    }
    commands = list(_routing_commands(step["run"]))
    assert all(
        re.search(r"--environment\s+\"?\$\{MODAL_ENVIRONMENT\}", command)
        for command in commands
    ), "Routing writes must explicitly use the proven MODAL_ENVIRONMENT"
    return _resolve_environment(environment["MODAL_ENVIRONMENT"], inputs)


def _expanded_writers(path, workflows, inputs=None, chain=()):
    workflow = workflows[path]
    inputs = inputs or {}
    for job_id, job in workflow["jobs"].items():
        job_chain = (*chain, (path, job_id))
        strategy = job.get("strategy", {})
        if job_id in _writer_jobs(path, workflows) and "matrix" in strategy:
            assert strategy.get("max-parallel") == 1, (
                f"Routing writer {job_chain} has a matrix which can run in parallel"
            )
        for step in _writing_steps(job):
            yield job_chain, _writer_environment(workflow, job, step, inputs)
        called = _called_workflow(job, workflows)
        if called:
            yield from _expanded_writers(
                called, workflows, inputs=job.get("with", {}), chain=job_chain
            )


def _depends_on(workflow: dict, job_id: str, predecessor: str) -> bool:
    needs = workflow["jobs"][job_id].get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    return predecessor in needs or any(
        _depends_on(workflow, needed, predecessor) for needed in needs
    )


def _ordered(left, right, workflows):
    for (path, left_job), (other_path, right_job) in zip(left, right):
        assert path == other_path
        if left_job != right_job:
            return _depends_on(workflows[path], left_job, right_job) or _depends_on(
                workflows[path], right_job, left_job
            )
    # Steps in one job run sequentially.
    return True


def test_jobs_never_write_one_modal_environment_in_parallel():
    workflows = _workflows()
    for path in _runnable_writer_workflows(workflows):
        writers = list(_expanded_writers(path, workflows))
        for (left, left_envs), (right, right_envs) in itertools.combinations(
            writers, 2
        ):
            assert not left_envs & right_envs or _ordered(left, right, workflows), (
                f"Parallel routing writers {left} and {right} share "
                f"Modal environment {left_envs & right_envs}"
            )


def test_stage12_callers_propagate_distinct_modal_routing_environments():
    workflows = _workflows()
    path = ".github/workflows/stage12-deploy.yml"
    jobs = workflows[path]["jobs"]
    staging = "deploy-stage12-staging"
    production = "deploy-stage12-production"
    assert jobs[staging]["with"]["modal_environment"] == "staging"
    assert jobs[production]["with"]["modal_environment"] == "main"
    writers = list(_expanded_writers(path, workflows))
    assert writers
    for chain, environments in writers:
        expected = "staging" if chain[0][1] == staging else "main"
        assert environments == {expected}
    # The reusable input reaches each writing step's environment and then its
    # --environment argument; these jobs can safely share validate-selection.
    assert jobs[staging]["needs"] == jobs[production]["needs"] == "validate-selection"


@pytest.mark.parametrize(
    ("arguments", "writes"),
    [
        ("--app-name app --policyengine-version 1.0", True),
        ("--apply-plan plan.json", True),
        ("--restore-plan plan.json", True),
        ("--restore-latest 1.0", True),
        ("--app-name app --plan-out plan.json", False),
        ("--app-name app --dry-run", False),
        ("--restore-plan plan.json --dry-run", True),
        ("--apply-plan plan.json --dry-run", True),
        ("--restore-latest 1.0 --dry-run", True),
        ("--seed-active-from-legacy --plan-out plan.json", True),
        ("--print-identity", False),
        ("--print-latest", False),
    ],
)
def test_writer_discovery_distinguishes_writing_and_read_only_modes(arguments, writes):
    command = f"uv run python -m {REGISTRY_COMMAND} {arguments}"
    assert bool(list(_writing_steps({"steps": [{"run": command}]}))) == writes


def test_read_only_invocation_cannot_hide_a_writer_in_the_same_step():
    script = (
        f"uv run python -m {REGISTRY_COMMAND} --print-identity\n"
        f"uv run python -m {REGISTRY_COMMAND} --restore-latest 1.0\n"
    )
    assert list(_writing_steps({"steps": [{"run": script}]}))


@pytest.mark.parametrize("separator", [";", "&&", "||", "|"])
def test_read_only_invocation_cannot_hide_a_same_line_default_publish(separator):
    script = (
        f"uv run python -m {REGISTRY_COMMAND} --print-identity {separator} "
        f"uv run python -m {REGISTRY_COMMAND} --app-name app --policyengine-version 1.0"
    )
    assert list(_writing_steps({"steps": [{"run": script}]}))


@pytest.mark.parametrize(
    "uses",
    [
        "./.github/workflows/reusable.yml",
        "PolicyEngine/policyengine-sim-api/.github/workflows/reusable.yml@main",
        "policyengine/policyengine-sim-api/.github/workflows/reusable.yml@ba12f73c",
    ],
)
def test_writer_discovery_follows_canonical_reusable_workflow_references(uses):
    workflows = {
        "caller.yml": {"jobs": {"release": {"uses": uses}}},
        ".github/workflows/reusable.yml": {
            "jobs": {
                "publish": {"steps": [{"run": f"uv run python -m {REGISTRY_COMMAND}"}]}
            }
        },
    }
    assert _writer_jobs("caller.yml", workflows) == {"release"}


def test_writer_discovery_includes_file_invocations_and_shell_arrays():
    script = """args=(
  --environment "${MODAL_ENVIRONMENT}"
  --restore-latest 1.0
)
uv run python src/modal/utils/update_version_registry.py "${args[@]}"
"""
    assert list(_writing_steps({"steps": [{"run": script}]}))


def test_writer_discovery_follows_nested_reusable_workflows():
    workflows = {
        "caller.yml": {"jobs": {"release": {"uses": "./.github/workflows/outer.yml"}}},
        ".github/workflows/outer.yml": {
            "jobs": {"release": {"uses": "./.github/workflows/inner.yml"}}
        },
        ".github/workflows/inner.yml": {
            "jobs": {
                "publish": {"steps": [{"run": f"uv run python -m {REGISTRY_COMMAND}"}]}
            }
        },
    }
    assert _writer_jobs("caller.yml", workflows) == {"release"}
