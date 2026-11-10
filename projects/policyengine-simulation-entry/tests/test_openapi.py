from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Literal, TypedDict, cast

from policyengine_simulation_entry.app import create_app

type HttpMethod = Literal["get", "post", "put", "patch", "delete"]


class OpenAPIOperation(TypedDict, total=False):
    operationId: str
    security: list[object]


type OpenAPIPathMap = dict[str, dict[str, OpenAPIOperation]]


class OpenAPIComponents(TypedDict):
    schemas: dict[str, object]


class OpenAPIDocument(TypedDict):
    paths: OpenAPIPathMap
    components: OpenAPIComponents


EXPECTED_ROUTES = {
    ("GET", "/budget-window-jobs/{batch_job_id}"),
    ("GET", "/household-calculations/{household_job_id}"),
    ("GET", "/health"),
    ("GET", "/jobs/{job_id}"),
    ("GET", "/ready"),
    ("GET", "/versions"),
    ("GET", "/versions/{kind}"),
    ("POST", "/household-calculations"),
    ("POST", "/ping"),
    ("POST", "/simulate/economy/budget-window"),
    ("POST", "/simulate/economy/comparison"),
}


def methods(spec: OpenAPIDocument) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }


def operation_ids(spec: OpenAPIDocument) -> dict[tuple[str, str], str]:
    return {
        (method.upper(), path): operation["operationId"]
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }


# The household calculation routes, which only the Cloud Run entrypoint has.
HOUSEHOLD_PATHS = (
    "/household-calculations",
    "/household-calculations/{household_job_id}",
)
HOUSEHOLD_SCHEMAS = (
    "HouseholdCalculationAccepted",
    "HouseholdCalculationError",
    "HouseholdCalculationRequest",
    "HouseholdCalculationState",
    "HouseholdJobStatus",
    "HouseholdRequestReason",
    "HouseholdRequestUnprocessable",
)


def normalized_compatibility_paths(spec: OpenAPIDocument) -> OpenAPIPathMap:
    """Remove intentional Cloud Run and additive SPM error documentation."""
    paths = deepcopy(spec["paths"])
    paths.pop("/ready", None)
    for path in HOUSEHOLD_PATHS:
        paths.pop(path, None)
    for operations in paths.values():
        for method, operation in operations.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                operation.pop("security", None)
    for path, description in {
        "/simulate/economy/comparison": "Invalid request (unknown country/version)",
        "/simulate/economy/budget-window": (
            "Invalid request (unknown country/version/year)"
        ),
    }.items():
        paths[path]["post"]["responses"]["400"] = {"description": description}
    for path in ("/jobs/{job_id}", "/budget-window-jobs/{batch_job_id}"):
        paths[path]["get"]["responses"].pop("400", None)
    return paths


def test_canonical_spm_extensions_are_public():
    spec = create_app().openapi()
    schemas = spec["components"]["schemas"]
    assert schemas["SPMSelection"]["additionalProperties"] is False
    assert all(
        "default" not in field
        for field in schemas["SPMSelection"]["properties"].values()
    )
    assert "spm" in schemas["SimulationRequest"]["properties"]
    assert "spm" in schemas["BudgetWindowBatchRequest"]["properties"]
    assert "spm_provenance" in schemas["SingleYearMacroOutput"]["properties"]
    assert "spm_provenance" in schemas["BudgetWindowAnnualImpact"]["properties"]
    assert "spm_config" not in schemas["SingleYearMacroOutput"]["properties"]
    assert "spm_config" not in schemas["BudgetWindowAnnualImpact"]["properties"]
    assert "SPMResolvedConfiguration" not in schemas
    assert all(
        schemas["SPMRuntimeVersions"]["properties"][field]["minLength"] == 1
        for field in (
            "policyengine",
            "policyengine-core",
            "policyengine-us",
            "spm-calculator",
        )
    )
    for path in ("/jobs/{job_id}", "/budget-window-jobs/{batch_job_id}"):
        assert "400" in spec["paths"][path]["get"]["responses"]


def test_local_area_breakdowns_are_public():
    schemas = create_app().openapi()["components"]["schemas"]

    breakdown = schemas["SimulationRequest"]["properties"]["breakdown"]
    assert breakdown["anyOf"] == [
        {"type": "string", "const": "local_areas"},
        {"type": "null"},
    ]
    assert schemas["LocalAreaBreakdownOutput"]["properties"]["kind"]["const"] == (
        "local_areas"
    )
    assert {"$ref": "#/components/schemas/LocalAreaBreakdownOutput"} in schemas[
        "JobStatusResponse"
    ]["properties"]["result"]["anyOf"]


def test_local_area_breakdowns_may_list_the_withheld_areas_of_each_level():
    breakdown = create_app().openapi()["components"]["schemas"][
        "LocalAreaBreakdownOutput"
    ]

    for field in ("constituency_withheld_codes", "local_authority_withheld_codes"):
        assert breakdown["properties"][field]["anyOf"] == [
            {"type": "array", "items": {"type": "string"}},
            {"type": "null"},
        ]
        # Optional, so results from executors that do not report them validate.
        assert field not in breakdown["required"]


def test_readiness_documents_the_simulation_database_check():
    spec = create_app().openapi()
    readiness = spec["paths"]["/ready"]["get"]["responses"]["200"]
    schema = readiness["content"]["application/json"]["schema"]
    database = spec["components"]["schemas"]["SimulationDatabaseReadiness"]

    assert schema == {
        "$ref": "#/components/schemas/SimulationEntrypointReadinessResponse"
    }
    assert set(database["properties"]) == {"reachable", "has_required_privileges"}


def test_route_table_is_frozen():
    assert methods(cast("OpenAPIDocument", create_app().openapi())) == EXPECTED_ROUTES


def test_old_gateway_routes_are_all_present():
    gateway_spec_path = (
        Path(__file__).resolve().parents[2]
        / "policyengine-simulation-gateway"
        / "tests"
        / "golden"
        / "openapi.json"
    )
    gateway_spec = cast(
        "OpenAPIDocument",
        json.loads(gateway_spec_path.read_text()),
    )
    cloud_run_routes = methods(cast("OpenAPIDocument", create_app().openapi()))

    assert methods(gateway_spec) <= cloud_run_routes


def test_normalized_contract_matches_old_gateway():
    gateway_spec_path = (
        Path(__file__).resolve().parents[2]
        / "policyengine-simulation-gateway"
        / "tests"
        / "golden"
        / "openapi.json"
    )
    gateway_spec = cast(
        "OpenAPIDocument",
        json.loads(gateway_spec_path.read_text()),
    )
    cloud_run_spec = cast("OpenAPIDocument", create_app().openapi())

    assert normalized_compatibility_paths(
        cloud_run_spec
    ) == normalized_compatibility_paths(gateway_spec)
    cloud_run_schemas = deepcopy(cloud_run_spec["components"]["schemas"])
    # The entrypoint's readiness response adds the simulation database check.
    cloud_run_schemas.pop("SimulationEntrypointReadinessResponse")
    cloud_run_schemas.pop("SimulationDatabaseReadiness")
    cloud_run_schemas.pop("SimulationErrorResponse")
    for schema_name in HOUSEHOLD_SCHEMAS:
        cloud_run_schemas.pop(schema_name)
    assert cloud_run_schemas == gateway_spec["components"]["schemas"]
    gateway_operation_ids = operation_ids(gateway_spec)
    cloud_run_operation_ids = operation_ids(cloud_run_spec)
    assert {
        key: cloud_run_operation_ids[key] for key in gateway_operation_ids
    } == gateway_operation_ids


def _responses(spec: dict, method: str, path: str) -> dict[str, dict]:
    return spec["paths"][path][method]["responses"]


def _response_schema(response: dict) -> dict:
    return response["content"]["application/json"]["schema"]


def test_household_routes_declare_every_answer():
    spec = create_app().openapi()
    error = {"$ref": "#/components/schemas/HouseholdCalculationError"}
    unprocessable = {"$ref": "#/components/schemas/HouseholdRequestUnprocessable"}

    submit = _responses(spec, "post", "/household-calculations")
    read = _responses(spec, "get", "/household-calculations/{household_job_id}")

    assert set(submit) == {"202", "400", "403", "422", "503"}
    assert _response_schema(submit["202"]) == {
        "$ref": "#/components/schemas/HouseholdCalculationAccepted"
    }
    assert _response_schema(submit["400"]) == error
    assert _response_schema(submit["422"]) == unprocessable
    assert _response_schema(submit["503"]) == error
    assert set(read) == {"200", "403", "404", "422", "503"}
    assert _response_schema(read["200"]) == {
        "$ref": "#/components/schemas/HouseholdCalculationState"
    }
    assert _response_schema(read["404"]) == error
    assert _response_schema(read["422"]) == unprocessable
    assert _response_schema(read["503"]) == error


def test_household_routes_name_their_operations():
    spec = create_app().openapi()

    assert operation_ids(spec)[("POST", "/household-calculations")] == (
        "submit_household_calculation"
    )
    assert operation_ids(spec)[
        ("GET", "/household-calculations/{household_job_id}")
    ] == ("get_household_calculation")


def test_a_household_calculation_request_names_its_fields_only():
    schemas = create_app().openapi()["components"]["schemas"]
    request = schemas["HouseholdCalculationRequest"]

    assert set(request["properties"]) == {
        "country",
        "household_id",
        "policy_id",
        "add_missing_variables",
        "spm",
    }
    assert set(request["required"]) == {"country", "household_id"}
    assert request["additionalProperties"] is False
    assert schemas["HouseholdJobStatus"]["enum"] == [
        "pending",
        "running",
        "succeeded",
        "failed",
    ]
