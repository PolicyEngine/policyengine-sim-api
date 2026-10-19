"""Workflow concurrency is the routing publisher's only serialization primitive."""

from __future__ import annotations

import ast
import itertools
import re
import shlex
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
REGISTRY_MODULE = (
    REPOSITORY_ROOT
    / "projects/policyengine-simulation-executor/src/modal/utils"
    / "update_version_registry.py"
)
REGISTRY_COMMAND = "src.modal.utils.update_version_registry"
REGISTRY_INVOCATION = re.compile(
    r"(?:src\.modal\.utils\.update_version_registry|"
    r"(?:[\w./-]*/)?update_version_registry\.py)\b"
)
ROUTING_LIBRARY_REFERENCE = re.compile(
    r"\b(?:update_version_registry|apply_routing_plan|restore_routing_plans|"
    r"restore_routing_plan|restore_routing_state|publish_routing_state|"
    r"record_routing_plan|complete_routing_plan|finalize_routing_publication)\b"
)
PROTECTED_SHELL_ENVIRONMENT = frozenset(
    {"MODAL_ENVIRONMENT", "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF"}
)
WRITING_OPTIONS = frozenset(
    {
        "--apply-plan",
        "--restore-plan",
        "--restore-latest",
        "--seed-active-from-legacy",
        "--record-plan",
        "--complete-plan",
        "--finalize-publication",
    }
)
READ_ONLY_OPTIONS = frozenset(
    {
        "--plan-out",
        "--dry-run",
        "--print-identity",
        "--print-latest",
        "--print-pending-plan",
    }
)
VALUE_OPTIONS = frozenset(
    {
        "--app-name",
        "--policyengine-version",
        "--us-version",
        "--uk-version",
        "--environment",
        "--plan-out",
        "--apply-plan",
        "--restore-plan",
        "--publication-id",
        "--restore-latest",
        "--expected-latest",
        "--record-plan",
        "--complete-plan",
        "--publication-run",
        "--release-environment",
        "--gates-passed",
        "--github-output",
    }
)
ROUTING_OPTIONS = (
    WRITING_OPTIONS | READ_ONLY_OPTIONS | VALUE_OPTIONS | {"--force-latest"}
)


def _workflows() -> dict[str, dict]:
    return {
        path.relative_to(REPOSITORY_ROOT).as_posix(): yaml.safe_load(path.read_text())
        for path in sorted(WORKFLOW_DIRECTORY.iterdir())
        if path.suffix in {".yml", ".yaml"}
    }


def _shell_tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=False, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    tokens = list(lexer)
    for token in tokens:
        assert "$" not in token or (token.startswith('"') and token.endswith('"')), (
            f"Unsupported unquoted or literal routing shell expansion: {token}"
        )
    return [
        shlex.split(token)[0] if token[:1] in {"'", '"'} else token for token in tokens
    ]


def _routing_commands(script: str):
    """Inspect foreground CLI argv in execution order, refusing unknown forms.

    The supported subset is explicit CLI commands and previously declared
    argument arrays. Conditional array appends are conservatively included.
    Importing the library or using shell wrappers needs a deliberate extension
    here, rather than silently escaping discovery or the ordering proof.
    """
    script = re.sub(r"\\\n\s*", " ", script)
    script = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    if not ROUTING_LIBRARY_REFERENCE.search(script):
        return
    for variable in PROTECTED_SHELL_ENVIRONMENT:
        # Shell builtins remove quotes from assignment operands, so both
        # export "MODAL_ENVIRONMENT"=main and export "MODAL_ENVIRONMENT=main"
        # override the same proven environment as an unquoted assignment.
        shell_name = rf"['\"]?{variable}['\"]?"
        assert not re.search(
            rf"(?:^|[\s;]){shell_name}\s*\+?=|\bunset\s+{shell_name}\b|"
            rf"\bread\s+{shell_name}\b|\bprintf\s+-v\s+{shell_name}\b",
            script,
        ), f"Shell overrides the proven routing environment or write guard: {variable}"

    # Preserve source order: a declaration after a command cannot prove that
    # command's argv. The repository uses only literal array assignments.
    logical_lines = re.sub(
        r"(\b\w+\+?=\()([^)]*)(\))",
        lambda match: "".join(match.groups()).replace("\n", " "),
        script,
    ).splitlines()
    arrays: dict[str, list[str]] = {}
    for line in logical_lines:
        assignment = re.fullmatch(r"\s*(\w+)(\+?)=\((.*?)\)\s*", line)
        if assignment:
            name, append, values = assignment.groups()
            if append:
                assert name in arrays, f"Cannot append unresolved routing array {name}"
                arrays[name] += _shell_tokens(values)
            else:
                arrays[name] = _shell_tokens(values)
            continue
        if not ROUTING_LIBRARY_REFERENCE.search(line):
            continue
        assert REGISTRY_INVOCATION.search(line), (
            f"Routing library calls must use the guarded foreground CLI: {line}"
        )
        assert "$(" not in line and "`" not in line, (
            f"Unsupported routing command substitution: {line}"
        )
        tokens = _shell_tokens(line)
        assert not any(
            token in {"{", "}"}
            or (
                re.fullmatch(r"[;&|()]+", token) and token not in {";", "&&", "||", "|"}
            )
            for token in tokens
        ), f"Routing commands must run in the foreground without shell grouping: {line}"
        commands = [
            list(group)
            for key, group in itertools.groupby(
                tokens, key=lambda token: token in {";", "&&", "||", "|"}
            )
            if not key
        ]
        for command in commands:
            invocations = [
                index
                for index, token in enumerate(command)
                if REGISTRY_INVOCATION.fullmatch(token)
            ]
            if not invocations:
                assert not ROUTING_LIBRARY_REFERENCE.search(" ".join(command)), (
                    f"Unsupported inline routing library invocation: {line}"
                )
                continue
            assert len(invocations) == 1, (
                f"Unsupported nested routing invocation: {line}"
            )
            index = invocations[0]
            prefix = command[:index]
            if command[index] == REGISTRY_COMMAND:
                assert prefix in (["uv", "run", "python", "-m"], ["python", "-m"]), (
                    f"Unsupported routing shell prefix: {prefix}"
                )
            else:
                assert prefix in (["uv", "run", "python"], ["python"]), (
                    f"Unsupported routing shell prefix: {prefix}"
                )
            arguments = []
            for token in command[index + 1 :]:
                array = re.fullmatch(r"\$\{(\w+)\[@\]\}", token)
                if array:
                    name = array.group(1)
                    assert name in arrays, (
                        f"Cannot resolve routing argument array {name}"
                    )
                    arguments.extend(arrays[name])
                else:
                    arguments.append(token)
            _command_options(tuple(arguments))
            yield tuple(arguments)


def _command_options(arguments: tuple[str, ...]) -> set[str]:
    """Parse option positions; an option-looking value is not a CLI mode."""
    options = set()
    index = 0
    while index < len(arguments):
        option, equals, value = arguments[index].partition("=")
        assert option in ROUTING_OPTIONS, (
            f"Unsupported routing option or shell form: {arguments[index]}"
        )
        options.add(option)
        index += 1
        if option in VALUE_OPTIONS:
            if equals:
                assert value, f"Routing option has no value: {option}"
            else:
                start = index
                while index < len(arguments) and not arguments[index].startswith("--"):
                    index += 1
                    if option != "--restore-plan":
                        break
                assert index > start, f"Routing option has no value: {option}"
        else:
            assert not equals, f"Unexpected value for routing flag: {option}"
    return options


def _command_writes(arguments: tuple[str, ...]) -> bool:
    options = _command_options(arguments)
    return bool(options & WRITING_OPTIONS) or not options & READ_ONLY_OPTIONS


def _writing_steps(job: dict):
    for step in job.get("steps", []):
        for arguments in _routing_commands(step.get("run", "")):
            if _command_writes(arguments):
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
    assert not (
        environment.keys() & (PROTECTED_SHELL_ENVIRONMENT - {"MODAL_ENVIRONMENT"})
    ), "Workflow environment must not override the routing write guard"
    commands = list(_routing_commands(step["run"]))
    for line in re.sub(r"\\\n\s*", " ", step["run"]).splitlines():
        if (
            not line.lstrip().startswith("#")
            and REGISTRY_INVOCATION.search(line)
            and "|" in _shell_tokens(line)
        ):
            assert len(REGISTRY_INVOCATION.findall(line)) <= 1, (
                "Routing pipeline commands can execute in parallel; "
                "use foreground sequential commands"
            )
    for arguments in commands:
        values = []
        for index, argument in enumerate(arguments):
            if argument == "--environment":
                assert index + 1 < len(arguments), "Routing environment has no value"
                values.append(arguments[index + 1])
            elif argument.startswith("--environment="):
                values.append(argument.partition("=")[2])
        assert values == ["${MODAL_ENVIRONMENT}"], (
            "Routing writes must explicitly use the proven MODAL_ENVIRONMENT "
            "exactly once"
        )
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
        ("--record-plan plan.json", True),
        ("--complete-plan plan.json", True),
        (
            (
                "--finalize-publication --publication-run 744 "
                "--release-environment prod --gates-passed true "
                "--github-output github-output.txt"
            ),
            True,
        ),
        (
            "--finalize-publication --release-environment beta --gates-passed false",
            True,
        ),
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
        ("--print-pending-plan", False),
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


def _environment_for_script(script: str, extra_environment=None):
    return _writer_environment(
        {},
        {},
        {
            "env": {"MODAL_ENVIRONMENT": "staging", **(extra_environment or {})},
            "run": script,
        },
        {},
    )


@pytest.mark.parametrize("variable", sorted(PROTECTED_SHELL_ENVIRONMENT))
@pytest.mark.parametrize(
    "form",
    [
        "{variable}=main\n",
        "export {variable}=main\n",
        "{variable}=main ",
        'export "{variable}"=main\n',
        "export '{variable}'=main\n",
        'export "{variable}=main"\n',
        "export '{variable}=main'\n",
    ],
)
def test_routing_shell_cannot_override_proven_environment_or_write_guard(
    variable, form
):
    script = form.format(variable=variable) + (
        f"uv run python -m {REGISTRY_COMMAND} "
        '--environment "${MODAL_ENVIRONMENT}" --apply-plan plan.json'
    )
    with pytest.raises(AssertionError, match="Shell overrides"):
        _environment_for_script(script)


@pytest.mark.parametrize(
    "variable", sorted(PROTECTED_SHELL_ENVIRONMENT - {"MODAL_ENVIRONMENT"})
)
def test_routing_yaml_environment_cannot_override_write_guard(variable):
    script = (
        f"uv run python -m {REGISTRY_COMMAND} "
        '--environment "${MODAL_ENVIRONMENT}" --apply-plan plan.json'
    )
    with pytest.raises(AssertionError, match="must not override"):
        _environment_for_script(script, {variable: "forged"})


@pytest.mark.parametrize("duplicate", ["--environment main", "--environment=main"])
@pytest.mark.parametrize("in_array", [False, True])
def test_routing_environment_checks_effective_argv_and_rejects_duplicates(
    duplicate, in_array
):
    arguments = (
        f'--environment "${{MODAL_ENVIRONMENT}}" {duplicate} --apply-plan plan.json'
    )
    script = f"uv run python -m {REGISTRY_COMMAND} {arguments}"
    if in_array:
        script = (
            f'args=({arguments})\nuv run python -m {REGISTRY_COMMAND} "${{args[@]}}"'
        )
    with pytest.raises(AssertionError, match="exactly once"):
        _environment_for_script(script)


def test_routing_array_expansion_uses_the_declaration_before_execution():
    script = (
        "args=(--environment main --apply-plan plan.json)\n"
        f'uv run python -m {REGISTRY_COMMAND} "${{args[@]}}"\n'
        'args=(--environment "${MODAL_ENVIRONMENT}" --apply-plan plan.json)\n'
    )
    with pytest.raises(AssertionError, match="exactly once"):
        _environment_for_script(script)


def test_routing_array_cannot_be_declared_after_execution():
    script = (
        f'uv run python -m {REGISTRY_COMMAND} "${{args[@]}}"\n'
        'args=(--environment "${MODAL_ENVIRONMENT}" --apply-plan plan.json)\n'
    )
    with pytest.raises(AssertionError, match="Cannot resolve routing argument array"):
        _environment_for_script(script)


@pytest.mark.parametrize("mode", ["--apply-plan", "--restore-plan"])
def test_routing_writers_cannot_run_in_background(mode):
    script = (
        f"uv run python -m {REGISTRY_COMMAND} "
        f'--environment "${{MODAL_ENVIRONMENT}}" {mode} plan.json &\nwait\n'
    )
    with pytest.raises(AssertionError, match="foreground"):
        _environment_for_script(script)


def test_routing_pipeline_cannot_make_writers_parallel():
    command = (
        f"uv run python -m {REGISTRY_COMMAND} "
        '--environment "${MODAL_ENVIRONMENT}" --apply-plan plan.json'
    )
    script = command + " | " + command.replace("--apply-plan", "--restore-plan")
    with pytest.raises(
        AssertionError, match="pipeline commands can execute in parallel"
    ):
        _environment_for_script(script)


@pytest.mark.parametrize(
    "script",
    [
        f"env uv run python -m {REGISTRY_COMMAND} --apply-plan plan.json",
        f'bash -c "uv run python -m {REGISTRY_COMMAND} --apply-plan plan.json"',
        f'result="$(uv run python -m {REGISTRY_COMMAND} --apply-plan plan.json)"',
        f"(uv run python -m {REGISTRY_COMMAND} --apply-plan plan.json)",
        f"{{ uv run python -m {REGISTRY_COMMAND} --apply-plan plan.json; }}",
        f'uv run python -c "import {REGISTRY_COMMAND}"',
        f"uv run python -m {REGISTRY_COMMAND} --dry-run $flags",
        (
            f"uv run python -m {REGISTRY_COMMAND} --environment $MODAL_ENVIRONMENT "
            "--apply-plan plan.json"
        ),
        (
            f"uv run python -m {REGISTRY_COMMAND} --environment "
            "'${MODAL_ENVIRONMENT}' --apply-plan plan.json"
        ),
    ],
)
def test_routing_discovery_rejects_unsupported_shell_forms(script):
    with pytest.raises(AssertionError, match="Unsupported|foreground"):
        list(_routing_commands(script))


@pytest.mark.parametrize(
    "writer",
    [
        "apply_routing_plan",
        "restore_routing_plans",
        "restore_routing_plan",
        "restore_routing_state",
        "publish_routing_state",
        "finalize_routing_publication",
    ],
)
def test_inline_python_cannot_bypass_routing_cli_discovery(writer):
    script = (
        "uv run python - <<'PYTHON'\n"
        "from src.modal.utils import update_version_registry as registry\n"
        f"registry.{writer}(environment='main')\n"
        "PYTHON\n"
    )
    with pytest.raises(AssertionError, match="guarded foreground CLI"):
        list(_writing_steps({"steps": [{"run": script}]}))


def test_finalizer_reference_cannot_bypass_routing_cli_discovery():
    script = 'uv run python -c "routing.finalize_routing_publication()"'
    with pytest.raises(AssertionError, match="guarded foreground CLI"):
        list(_writing_steps({"steps": [{"run": script}]}))


def test_argument_value_cannot_hide_a_default_writer():
    script = f"uv run python -m {REGISTRY_COMMAND} --app-name app--dry-run"
    assert list(_writing_steps({"steps": [{"run": script}]}))
