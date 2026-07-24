from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from policyengine_simulation_entry.app import create_app


EXPECTED_ROUTES = {
    ("GET", "/budget-window-jobs/{batch_job_id}"),
    ("GET", "/health"),
    ("GET", "/jobs/{job_id}"),
    ("GET", "/ready"),
    ("GET", "/versions"),
    ("GET", "/versions/{kind}"),
    ("POST", "/ping"),
    ("POST", "/simulate/economy/budget-window"),
    ("POST", "/simulate/economy/comparison"),
}


def methods(spec: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }


def operation_ids(spec: dict) -> dict[tuple[str, str], str]:
    return {
        (method.upper(), path): operation["operationId"]
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }


def normalized_compatibility_paths(spec: dict) -> dict:
    """Remove the two intentional Cloud Run-only OpenAPI additions."""
    paths = deepcopy(spec["paths"])
    paths.pop("/ready", None)
    for operations in paths.values():
        for method, operation in operations.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                operation.pop("security", None)
    return paths


def test_route_table_is_frozen():
    assert methods(create_app().openapi()) == EXPECTED_ROUTES


def test_old_gateway_routes_are_all_present():
    gateway_spec_path = (
        Path(__file__).resolve().parents[2]
        / "policyengine-simulation-gateway"
        / "tests"
        / "golden"
        / "openapi.json"
    )
    gateway_spec = json.loads(gateway_spec_path.read_text())
    cloud_run_routes = methods(create_app().openapi())

    assert methods(gateway_spec) <= cloud_run_routes


def test_normalized_contract_matches_old_gateway():
    gateway_spec_path = (
        Path(__file__).resolve().parents[2]
        / "policyengine-simulation-gateway"
        / "tests"
        / "golden"
        / "openapi.json"
    )
    gateway_spec = json.loads(gateway_spec_path.read_text())
    cloud_run_spec = create_app().openapi()

    assert normalized_compatibility_paths(
        cloud_run_spec
    ) == normalized_compatibility_paths(gateway_spec)
    assert (
        cloud_run_spec["components"]["schemas"] == gateway_spec["components"]["schemas"]
    )
    gateway_operation_ids = operation_ids(gateway_spec)
    cloud_run_operation_ids = operation_ids(cloud_run_spec)
    assert {
        key: cloud_run_operation_ids[key] for key in gateway_operation_ids
    } == gateway_operation_ids
