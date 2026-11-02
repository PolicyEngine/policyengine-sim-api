"""How the beta deploy spreads this suite over pytest-xdist workers.

``.github/scripts/simulation-run-integ-tests.sh`` runs the beta suite with
``--dist loadgroup``: xdist runs all tests of one ``xdist_group`` on one
worker, and every test without a group is a work unit of its own. Each
``beta_only`` test submits a calculation to beta and waits minutes for it;
every other test here takes seconds. So the other tests share one group, each
calculation test is a unit of its own, and the run has one worker per unit:
every calculation starts at once, and none waits behind another on the same
worker.
"""

import ast
import re
from pathlib import Path

import pytest

SIMULATION_TESTS_DIRECTORY = Path(__file__).parent
REPOSITORY_ROOT = SIMULATION_TESTS_DIRECTORY.parents[3]
RUN_SCRIPT = REPOSITORY_ROOT / ".github" / "scripts" / "simulation-run-integ-tests.sh"
SHORT_TESTS_GROUP = "short_tests"
# The region types policyengine.py's bundle manifest routes to the UK
# local-area dataset (its region_datasets entries for UK constituencies and
# local authorities).
UK_LOCAL_AREA_REGION_PREFIXES = ("constituency/", "local_authority/")


def _xdist_group_names(item: pytest.Item) -> list[str]:
    return [
        str(mark.args[0] if mark.args else mark.kwargs.get("name", "default"))
        for mark in item.iter_markers("xdist_group")
    ]


def _test_functions() -> list[ast.FunctionDef]:
    """Return the module-level test functions of every test module here."""
    test_functions: list[ast.FunctionDef] = []
    for module_path in sorted(SIMULATION_TESTS_DIRECTORY.glob("test_*.py")):
        module = ast.parse(module_path.read_text(encoding="utf-8"))
        test_functions.extend(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        )
    return test_functions


def _is_beta_only(test_function: ast.FunctionDef) -> bool:
    return any(
        ast.unparse(decorator) == "pytest.mark.beta_only"
        for decorator in test_function.decorator_list
    )


def _requests_uk_local_area_dataset(test_function: ast.FunctionDef) -> bool:
    """Return whether a request dict in the test routes to the local-area dataset."""
    for node in ast.walk(test_function):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (
                isinstance(key, ast.Constant)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                continue
            if key.value == "breakdown" and value.value == "local_areas":
                return True
            if key.value == "region" and value.value.startswith(
                UK_LOCAL_AREA_REGION_PREFIXES
            ):
                return True
    return False


def test_short_tests_share_one_group_and_each_calculation_is_its_own_unit(
    request: pytest.FixtureRequest,
):
    """Every collected test here without beta_only, and only those, is grouped."""
    collected_here = [
        item
        for item in request.session.items
        if item.path.is_relative_to(SIMULATION_TESTS_DIRECTORY)
    ]
    assert request.node in collected_here

    for item in collected_here:
        expected_groups = (
            [] if item.get_closest_marker("beta_only") else [SHORT_TESTS_GROUP]
        )
        assert _xdist_group_names(item) == expected_groups, item.nodeid


def test_the_beta_run_has_one_worker_per_work_unit():
    """The worker count is the number of calculation tests plus one."""
    worker_setting = re.search(
        r"^BETA_PYTEST_WORKERS=(\d+)$",
        RUN_SCRIPT.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert worker_setting is not None, f"BETA_PYTEST_WORKERS is not set in {RUN_SCRIPT}"
    calculation_tests = [
        test_function.name
        for test_function in _test_functions()
        if _is_beta_only(test_function)
    ]

    assert int(worker_setting.group(1)) == len(calculation_tests) + 1, (
        "Give each beta_only calculation test its own worker, plus one worker "
        f"for the {SHORT_TESTS_GROUP} group: {calculation_tests}"
    )


def test_one_test_downloads_the_uk_local_area_dataset():
    """Only one test makes a worker download the UK local-area dataset.

    The default UK dataset is in the executor's Modal image. A local-area
    breakdown, or a constituency or local-authority region, makes its worker
    container download ``microcosm_uk_2024_25_dense.h5`` from Hugging Face
    during the request, and policyengine.py fails on the first HTTP 429. With
    one such test, the parallel beta run downloads the file in one container,
    as the serial run did. A second such test would download it in a second
    container at the same time: give both the same ``xdist_group`` so they run
    one after the other.
    """
    downloading_tests = [
        test_function.name
        for test_function in _test_functions()
        if _requests_uk_local_area_dataset(test_function)
    ]

    assert len(downloading_tests) == 1, downloading_tests
