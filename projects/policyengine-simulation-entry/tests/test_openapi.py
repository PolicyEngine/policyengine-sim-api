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
    ("GET", "/reports/{report_id}"),
    ("GET", "/simulations/{simulation_id}"),
    ("GET", "/versions"),
    ("GET", "/versions/{kind}"),
    ("POST", "/household-calculations"),
    ("POST", "/ping"),
    ("POST", "/reports"),
    ("POST", "/reports/{report_id}/runs"),
    ("POST", "/simulate/economy/budget-window"),
    ("POST", "/simulate/economy/comparison"),
    ("POST", "/simulations"),
    ("POST", "/simulations/{simulation_id}/runs"),
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
    "HouseholdAxis",
    "HouseholdCalculationAccepted",
    "HouseholdCalculationError",
    "HouseholdCalculationRequest",
    "HouseholdCalculationState",
    "HouseholdJobStatus",
    "HouseholdRequestReason",
    "HouseholdRequestUnprocessable",
)


# The economy report and simulation routes, which only the Cloud Run
# entrypoint has.
ECONOMY_PATHS = (
    "/reports",
    "/reports/{report_id}",
    "/reports/{report_id}/runs",
    "/simulations",
    "/simulations/{simulation_id}",
    "/simulations/{simulation_id}/runs",
)
ECONOMY_SCHEMAS = (
    "BudgetWindowReportCreated",
    "BudgetWindowReportRequest",
    "BudgetWindowYearCreated",
    "EconomyError",
    "EconomyReportCreated",
    "EconomyReportRequest",
    "EconomyReportState",
    "EconomyRequestReason",
    "EconomyRequestUnprocessable",
    "EconomySimulationCreated",
    "EconomySimulationRequest",
    "EconomySimulationState",
    "HouseholdPolicyChoice",
    "HouseholdReportCreated",
    "HouseholdReportRequest",
    "PolicyChoice",
    "ReportRerunRequest",
    "ReportRunRead",
    "ReportRunStatus",
    "SimulationRunRead",
    "SimulationStatus",
)


def normalized_compatibility_paths(spec: OpenAPIDocument) -> OpenAPIPathMap:
    """Remove intentional Cloud Run and additive SPM error documentation."""
    paths = deepcopy(spec["paths"])
    paths.pop("/ready", None)
    for path in (*HOUSEHOLD_PATHS, *ECONOMY_PATHS):
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
    for schema_name in (*HOUSEHOLD_SCHEMAS, *ECONOMY_SCHEMAS):
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

    assert set(submit) == {"202", "400", "403", "422", "500", "503"}
    # A 202 may also be a job whose call Modal didn't confirm: poll it.
    assert "pending" in submit["202"]["description"]
    assert _response_schema(submit["202"]) == {
        "$ref": "#/components/schemas/HouseholdCalculationAccepted"
    }
    assert _response_schema(submit["400"]) == error
    assert _response_schema(submit["422"]) == unprocessable
    assert _response_schema(submit["503"]) == error
    assert set(read) == {"200", "403", "404", "422", "500", "503"}
    assert _response_schema(read["200"]) == {
        "$ref": "#/components/schemas/HouseholdCalculationState"
    }
    assert _response_schema(read["404"]) == error
    assert _response_schema(read["422"]) == unprocessable
    assert _response_schema(read["503"]) == error
    assert _response_schema(submit["500"]) == error
    assert _response_schema(read["500"]) == error
    code = spec["components"]["schemas"]["HouseholdCalculationError"]["properties"][
        "code"
    ]
    assert "job_state_unknown" not in code["enum"]
    assert "internal_error" in code["enum"]


def test_a_household_job_read_declares_only_the_answers_a_read_gives():
    """A read never refuses stored data or starts a Modal call."""
    read = _responses(
        create_app().openapi(), "get", "/household-calculations/{household_job_id}"
    )

    assert "stored" not in read["422"]["description"]
    assert "UUID" in read["422"]["description"]
    assert "Modal" not in read["503"]["description"]
    assert "database" in read["503"]["description"]


def test_every_household_503_declares_its_retry_after_header():
    spec = create_app().openapi()

    for method, path in (
        ("post", "/household-calculations"),
        ("get", "/household-calculations/{household_job_id}"),
    ):
        headers = _responses(spec, method, path)["503"]["headers"]
        assert headers["Retry-After"]["schema"] == {"type": "integer"}


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
        "axes",
        "spm",
    }
    # SPM settings are an object when given; null is refused.
    assert request["properties"]["spm"]["type"] == "object"
    assert set(schemas["HouseholdCalculationAccepted"]["properties"]) == {
        "job_id",
        "status",
    }
    assert set(request["required"]) == {"country", "household_id"}
    assert request["additionalProperties"] is False
    assert schemas["HouseholdJobStatus"]["enum"] == [
        "pending",
        "running",
        "succeeded",
        "failed",
    ]


def test_economy_routes_declare_every_answer():
    spec = create_app().openapi()
    error = {"$ref": "#/components/schemas/EconomyError"}
    unprocessable = {"$ref": "#/components/schemas/EconomyRequestUnprocessable"}

    for path, created in (
        (
            "/reports",
            (
                "EconomyReportCreated",
                "BudgetWindowReportCreated",
                "HouseholdReportCreated",
            ),
        ),
        ("/simulations", ("EconomySimulationCreated",)),
    ):
        responses = _responses(spec, "post", path)
        assert set(responses) == {"201", "400", "403", "422", "500", "503"}
        created_schema = _response_schema(responses["201"])
        assert created_schema.get("anyOf", [created_schema]) == [
            {"$ref": f"#/components/schemas/{name}"} for name in created
        ]
        for status_code in ("400", "500", "503"):
            assert _response_schema(responses[status_code]) == error
        assert _response_schema(responses["422"]) == unprocessable
        record = path.removeprefix("/").removesuffix("s")
        assert f"{record}_id" in responses["503"]["description"]
        assert "nothing was written" in responses["400"]["description"]
        # A create answers the record and run it ended on, joined or created.
        assert "joined" in responses["201"]["description"]
        assert "found or created" in responses["503"]["description"]
    error_properties = spec["components"]["schemas"]["EconomyError"]["properties"]
    assert {"code", "detail", "report_id", "simulation_id", "run_id"} == set(
        error_properties
    )
    codes = set(error_properties["code"]["enum"])
    assert {
        "unknown_region",
        "unknown_dataset",
        "unknown_dynamic",
        "unknown_catalog_version",
        "unknown_household",
        "no_household_function",
        "modal_call_refused",
        "internal_error",
    } <= codes
    unprocessable_codes = spec["components"]["schemas"]["EconomyRequestUnprocessable"][
        "properties"
    ]["code"]["enum"]
    assert "household_not_calculable" in unprocessable_codes


def test_every_economy_503_declares_its_retry_after_header():
    spec = create_app().openapi()

    for path in ECONOMY_PATHS:
        method = "get" if path.endswith("}") else "post"
        headers = _responses(spec, method, path)["503"]["headers"]
        assert headers["Retry-After"]["schema"] == {"type": "integer"}


def test_rerun_routes_declare_every_answer():
    spec = create_app().openapi()
    error = {"$ref": "#/components/schemas/EconomyError"}
    unprocessable = {"$ref": "#/components/schemas/EconomyRequestUnprocessable"}

    for path, rerun in (
        (
            "/reports/{report_id}/runs",
            (
                "EconomyReportCreated",
                "BudgetWindowReportCreated",
                "HouseholdReportCreated",
            ),
        ),
        ("/simulations/{simulation_id}/runs", ("EconomySimulationCreated",)),
    ):
        responses = _responses(spec, "post", path)
        assert set(responses) == {
            "201",
            "400",
            "403",
            "404",
            "409",
            "422",
            "500",
            "503",
        }
        rerun_schema = _response_schema(responses["201"])
        assert rerun_schema.get("anyOf", [rerun_schema]) == [
            {"$ref": f"#/components/schemas/{name}"} for name in rerun
        ]
        for status_code in ("400", "404", "409", "500", "503"):
            assert _response_schema(responses[status_code]) == error
        assert _response_schema(responses["422"]) == unprocessable
        assert "run_id" in responses["503"]["description"]
        # Both answer the run they ended on, created or joined.
        assert "joined" in responses["201"]["description"]
    # No re-run is refused while a run is under way: an unforced one joins
    # it, and a forced one is an extra run (C1 · 24's answers 3 and 8).
    for path in ("/reports/{report_id}/runs", "/simulations/{simulation_id}/runs"):
        conflict = _responses(spec, "post", path)["409"]
        assert "run_in_progress" not in conflict["description"]
        assert "rerun_not_supported" in conflict["description"]
    codes = set(
        spec["components"]["schemas"]["EconomyError"]["properties"]["code"]["enum"]
    )
    assert "rerun_not_supported" in codes
    assert "run_in_progress" not in codes
    # A household report's re-run names its household's own codes.
    report_answers = _responses(spec, "post", "/reports/{report_id}/runs")
    assert "unknown_household" in report_answers["400"]["description"]
    assert "household_not_calculable" in report_answers["422"]["description"]
    # A report stored asking for cliffs is refused until they exist.
    assert "cliffs_not_available" in report_answers["422"]["description"]
    simulation_answers = _responses(spec, "post", "/simulations/{simulation_id}/runs")
    assert "cliffs_not_available" not in simulation_answers["422"]["description"]
    report_rerun = spec["components"]["schemas"]["ReportRerunRequest"]
    assert set(report_rerun["properties"]) == {"force"}
    assert (
        "requestBody" not in spec["paths"]["/simulations/{simulation_id}/runs"]["post"]
    )
    names = operation_ids(cast("OpenAPIDocument", spec))
    assert names[("POST", "/reports/{report_id}/runs")] == "rerun_report"
    assert names[("POST", "/simulations/{simulation_id}/runs")] == "rerun_simulation"


def test_economy_routes_name_their_operations():
    names = operation_ids(cast("OpenAPIDocument", create_app().openapi()))

    assert names[("POST", "/reports")] == "create_report"
    assert names[("POST", "/simulations")] == "create_simulation"


def test_economy_requests_name_no_version_and_no_record_id():
    """The service runs its default worker's version and chooses every ID."""
    schemas = create_app().openapi()["components"]["schemas"]

    assert set(schemas["EconomyReportRequest"]["properties"]) == {
        "country",
        "report_type",
        "label",
        "description",
        "year",
        "region_code",
        "dataset_name",
        "baseline",
        "reform",
        "include_cliffs",
        "spm",
    }
    assert set(schemas["EconomySimulationRequest"]["properties"]) == {
        "country",
        "simulation_type",
        "year",
        "region_code",
        "dataset_name",
        "policy_id",
        "dynamic_id",
    }
    # Omitted, not null, when the caller gives no SPM settings.
    spm = schemas["EconomyReportRequest"]["properties"]["spm"]
    assert spm["$ref"] == "#/components/schemas/SPMSelection"
    assert "default" not in spm
    assert "spm" not in schemas["EconomyReportRequest"].get("required", [])


def test_a_reports_label_and_description_are_optional_and_never_stored():
    """Answer 6: each user's saved report holds them, in the Public API."""
    schemas = create_app().openapi()["components"]["schemas"]

    # A budget window's come with its keys (answer 8).
    for name in ("EconomyReportRequest", "HouseholdReportRequest"):
        request = schemas[name]
        assert {"label", "description"} <= set(request["properties"]), name
        assert "label" not in request.get("required", []), name
        assert "not stored" in request["properties"]["label"]["description"], name


def test_a_report_request_is_an_economy_report_a_budget_window_or_a_household():
    spec = create_app().openapi()
    body = spec["paths"]["/reports"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    schemas = spec["components"]["schemas"]

    assert body["discriminator"] == {
        "propertyName": "report_type",
        "mapping": {
            "economy": "#/components/schemas/EconomyReportRequest",
            "budget_window": "#/components/schemas/BudgetWindowReportRequest",
            "household": "#/components/schemas/HouseholdReportRequest",
        },
    }
    # A window sends its length and no cliff option, and names no version
    # or record ID either.
    assert set(schemas["BudgetWindowReportRequest"]["properties"]) == {
        "country",
        "report_type",
        "label",
        "description",
        "year",
        "budget_window_size",
        "region_code",
        "dataset_name",
        "baseline",
        "reform",
        "spm",
    }
    size = schemas["BudgetWindowReportRequest"]["properties"]["budget_window_size"]
    assert (size["minimum"], size["maximum"]) == (1, 75)


def test_a_household_report_names_its_household_and_each_sides_policy():
    schemas = create_app().openapi()["components"]["schemas"]

    # No year, region, dataset, dynamic, version or record ID, and no option:
    # a household report always adds the household's missing variables.
    household_report = schemas["HouseholdReportRequest"]
    assert set(household_report["properties"]) == {
        "country",
        "report_type",
        "label",
        "description",
        "household_id",
        "baseline",
        "reform",
    }
    assert set(household_report["required"]) == {
        "country",
        "report_type",
        "household_id",
        "baseline",
    }
    assert set(schemas["HouseholdPolicyChoice"]["properties"]) == {"policy_id"}
    assert set(schemas["HouseholdReportCreated"]["required"]) == {
        "report_id",
        "report_run_id",
        "status",
        "year",
        "baseline_simulation_id",
        "baseline_simulation_run_id",
        "reform_simulation_id",
        "reform_simulation_run_id",
    }


def test_economy_reads_declare_every_answer():
    spec = create_app().openapi()
    error = {"$ref": "#/components/schemas/EconomyError"}

    for path, state in (
        ("/reports/{report_id}", "EconomyReportState"),
        ("/simulations/{simulation_id}", "EconomySimulationState"),
    ):
        responses = _responses(spec, "get", path)
        assert set(responses) == {"200", "403", "404", "422", "500", "503"}
        assert _response_schema(responses["200"]) == {
            "$ref": f"#/components/schemas/{state}"
        }
        for status_code in ("404", "500", "503"):
            assert _response_schema(responses[status_code]) == error
        assert "Modal" not in responses["503"]["description"]
    names = operation_ids(cast("OpenAPIDocument", spec))
    assert names[("GET", "/reports/{report_id}")] == "get_report"
    assert names[("GET", "/simulations/{simulation_id}")] == "get_simulation"
