from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

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
    ("GET", "/health"),
    ("GET", "/jobs/{job_id}"),
    ("GET", "/ready"),
    ("GET", "/versions"),
    ("GET", "/versions/{kind}"),
    ("POST", "/ping"),
    ("POST", "/simulate/economy/budget-window"),
    ("POST", "/simulate/economy/comparison"),
    ("GET", "/v2/economy-reports/by-legacy-id/{legacy_report_id}"),
    ("GET", "/v2/economy-reports/{report_id}"),
    ("GET", "/v2/economy-reports/{report_id}/runs"),
    ("GET", "/v2/simulations/{simulation_id}"),
    ("POST", "/v2/economy-reports"),
    ("POST", "/v2/economy-reports/{report_id}/runs"),
    ("GET", "/v2/budget-window-reports/{report_id}"),
    ("GET", "/v2/budget-window-reports/{report_id}/runs"),
    ("POST", "/v2/budget-window-reports"),
    ("POST", "/v2/budget-window-reports/{report_id}/runs"),
}
# The v2 economy and budget-window report routes are the entrypoint's own; the
# old gateway has none of them or their bodies.
V2_SCHEMAS = (
    "BudgetWindowReportResponse",
    "BudgetWindowReportResult",
    "BudgetWindowReportSubmissionRequest",
    "BudgetWindowRunResponse",
    "BudgetWindowRunsResponse",
    "BudgetWindowTotalImpact",
    "BudgetWindowYearImpact",
    "BudgetWindowYearResponse",
    "ComparisonResponse",
    "EconomyErrorResponse",
    "EconomyRefusalResponse",
    "EconomyRequestFieldError",
    "EconomyReportResponse",
    "EconomyReportRunRequest",
    "EconomyReportRunResponse",
    "EconomyReportRunsResponse",
    "EconomyReportSubmissionRequest",
    "ReportStatus",
    "RunErrorResponse",
    "SimulationChoiceRequest",
    "SimulationResponse",
    "SimulationRunResponse",
)


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


def normalized_compatibility_paths(spec: OpenAPIDocument) -> OpenAPIPathMap:
    """Remove intentional Cloud Run and additive SPM error documentation."""
    paths = deepcopy(spec["paths"])
    paths.pop("/ready", None)
    for path in [path for path in paths if path.startswith("/v2/")]:
        paths.pop(path)
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


def test_readiness_documents_the_simulation_database_revision():
    spec = create_app().openapi()
    readiness = spec["paths"]["/ready"]["get"]["responses"]["200"]
    schema = readiness["content"]["application/json"]["schema"]
    database = spec["components"]["schemas"]["SimulationDatabaseReadiness"]

    assert schema == {
        "$ref": "#/components/schemas/SimulationEntrypointReadinessResponse"
    }
    assert set(database["properties"]) == {
        "reachable",
        "revision",
        "expected_revision",
    }


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
    # The entrypoint's readiness response adds its own database's revision.
    cloud_run_schemas.pop("SimulationEntrypointReadinessResponse")
    cloud_run_schemas.pop("SimulationDatabaseReadiness")
    cloud_run_schemas.pop("SimulationErrorResponse")
    for schema in V2_SCHEMAS:
        cloud_run_schemas.pop(schema)
    assert cloud_run_schemas == gateway_spec["components"]["schemas"]
    gateway_operation_ids = operation_ids(gateway_spec)
    cloud_run_operation_ids = operation_ids(cloud_run_spec)
    assert {
        key: cloud_run_operation_ids[key] for key in gateway_operation_ids
    } == gateway_operation_ids


def test_v2_economy_routes_are_public_for_the_generated_client():
    spec = cast("OpenAPIDocument", create_app().openapi())
    ids = operation_ids(spec)

    assert ids[("POST", "/v2/economy-reports")] == "submit_economy_report"
    assert ids[("GET", "/v2/economy-reports/{report_id}")] == "get_economy_report"
    assert ids[("POST", "/v2/economy-reports/{report_id}/runs")] == (
        "run_economy_report"
    )
    assert set(V2_SCHEMAS) <= set(spec["components"]["schemas"])


def test_v2_budget_window_results_have_models_of_their_own():
    """The old gateway's budget-window models stay the old gateway's."""
    spec = cast("dict[str, Any]", create_app().openapi())
    report = spec["components"]["schemas"]["BudgetWindowReportResponse"]

    assert report["properties"]["result"]["anyOf"][0] == {
        "$ref": "#/components/schemas/BudgetWindowReportResult"
    }
    year = spec["components"]["schemas"]["BudgetWindowYearImpact"]
    assert "spm_provenance" not in year["properties"]


def test_v2_budget_window_routes_are_public_for_the_generated_client():
    spec = cast("OpenAPIDocument", create_app().openapi())
    ids = operation_ids(spec)

    assert {
        ids[("POST", "/v2/budget-window-reports")],
        ids[("GET", "/v2/budget-window-reports/{report_id}")],
        ids[("POST", "/v2/budget-window-reports/{report_id}/runs")],
        ids[("GET", "/v2/budget-window-reports/{report_id}/runs")],
    } == {
        "submit_budget_window_report",
        "get_budget_window_report",
        "run_budget_window_report",
        "list_budget_window_report_runs",
    }


V2_ERROR_ANSWERS = {
    **{
        submission: {
            "422": "EconomyRefusalResponse",
            "503": "EconomyErrorResponse",
        }
        for submission in (
            ("POST", "/v2/economy-reports"),
            ("POST", "/v2/budget-window-reports"),
        )
    },
    **{
        operation: {
            "404": "EconomyErrorResponse",
            "422": "EconomyRefusalResponse",
            "503": "EconomyErrorResponse",
        }
        for operation in (
            ("GET", "/v2/economy-reports/{report_id}"),
            ("GET", "/v2/economy-reports/by-legacy-id/{legacy_report_id}"),
            ("POST", "/v2/economy-reports/{report_id}/runs"),
            ("GET", "/v2/economy-reports/{report_id}/runs"),
            ("GET", "/v2/simulations/{simulation_id}"),
            ("GET", "/v2/budget-window-reports/{report_id}"),
            ("POST", "/v2/budget-window-reports/{report_id}/runs"),
            ("GET", "/v2/budget-window-reports/{report_id}/runs"),
        )
    },
}
# The reads that rebuild a stored result also answer 500 when it cannot be.
for _result_read in (
    ("GET", "/v2/economy-reports/{report_id}"),
    ("GET", "/v2/economy-reports/by-legacy-id/{legacy_report_id}"),
    ("GET", "/v2/budget-window-reports/{report_id}"),
):
    V2_ERROR_ANSWERS[_result_read] = {
        **V2_ERROR_ANSWERS[_result_read],
        "500": "EconomyErrorResponse",
    }


def test_v2_routes_declare_each_error_answer_with_its_body():
    """The generated client parses sim's own refusals, not only FastAPI's."""
    spec = cast("dict[str, Any]", create_app().openapi())

    declared = {
        (method, path): {
            status: answer["content"]["application/json"]["schema"]["$ref"].rpartition(
                "/"
            )[2]
            for status, answer in spec["paths"][path][method.lower()][
                "responses"
            ].items()
            if not status.startswith("2")
        }
        for method, path in V2_ERROR_ANSWERS
    }

    assert declared == V2_ERROR_ANSWERS
    refusal_detail = spec["components"]["schemas"]["EconomyRefusalResponse"][
        "properties"
    ]["detail"]
    assert refusal_detail["anyOf"] == [
        {"type": "string"},
        {
            "items": {"$ref": "#/components/schemas/EconomyRequestFieldError"},
            "type": "array",
        },
    ]


def test_v2_budget_window_routes_describe_their_own_refusals():
    spec = cast("dict[str, Any]", create_app().openapi())
    submit = spec["paths"]["/v2/budget-window-reports"]["post"]["responses"]

    assert "budget-window reports" in submit["503"]["description"]
    assert "economy reports" not in submit["503"]["description"]
