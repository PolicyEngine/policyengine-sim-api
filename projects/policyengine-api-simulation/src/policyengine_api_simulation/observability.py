from __future__ import annotations

import os
from dataclasses import replace
from enum import StrEnum
from typing import Any

from fastapi import FastAPI
from policyengine_observability import (
    UNKNOWN_SEGMENT,
    ObservabilityConfig,
    ObservabilityRuntime,
    set_observability_runtime,
)
from policyengine_observability.adapters.fastapi import (
    init_fastapi_observability,
)


SERVICE_NAME = "policyengine-api-simulation"
SPAN_PREFIX = "simulation"
LOG_DESTINATIONS = ("stdout",)
LOGFIRE_STATUS = "legacy_candidate_for_replacement"
LOGFIRE_REPLACEMENT_CANDIDATE = "policyengine-observability"
SIMULATION_METRIC_ATTRIBUTE_KEYS = (
    "batch_job_id",
    "country",
    "function_call_id",
    "geography_code",
    "geography_type",
    "job_id",
    "modal_app_name",
    "modal_environment",
    "modal_function_name",
    "platform",
    "policyengine_version",
    "process_id",
    "region",
    "request_id",
    "resolved_app_name",
    "resolved_version",
    "run_id",
    "logfire_status",
    "logfire_replacement_candidate",
    "runtime_role",
    "scope",
    "simulation_year",
)


class SegmentName(StrEnum):
    UNKNOWN = UNKNOWN_SEGMENT

    REQUEST_PARSE = "request_parse"
    ROUTE_RESOLUTION = "route_resolution"
    POLICYENGINE_BUNDLE = "policyengine_bundle"
    MODAL_FUNCTION_LOOKUP = "modal_function_lookup"
    MODAL_FUNCTION_SPAWN = "modal_function_spawn"
    MODAL_DICT_READ = "modal_dict_read"
    MODAL_JOB_METADATA_WRITE = "modal_job_metadata_write"
    MODAL_JOB_STATUS_POLL = "modal_job_status_poll"

    CREDENTIAL_SETUP = "credential_setup"
    COUNTRY_MODULE_LOAD = "country_module_load"
    REGION_RESOLUTION = "region_resolution"
    DATASET_RESOLUTION = "dataset_resolution"
    DATASET_LOAD = "dataset_load"
    POLICY_NORMALIZATION = "policy_normalization"
    SIMULATION_BUILD = "simulation_build"
    CALCULATION = "calculation"
    RESPONSE_SERIALIZATION = "response_serialization"

    BUDGET_WINDOW_CONTEXT = "budget_window_context"
    BUDGET_WINDOW_STATE_LOAD = "budget_window_state_load"
    BUDGET_WINDOW_STATE_WRITE = "budget_window_state_write"
    BUDGET_WINDOW_CHILD_SPAWN = "budget_window_child_spawn"
    BUDGET_WINDOW_RESULT_PARSE = "budget_window_result_parse"
    BUDGET_WINDOW_AGGREGATION = "budget_window_aggregation"
    BUDGET_WINDOW_CHILD_REQUEST_BUILD = "budget_window_child_request_build"
    BUDGET_WINDOW_STATUS_SERIALIZATION = "budget_window_status_serialization"
    # NOTE: per-iteration poll/sleep segments were removed deliberately —
    # the scheduler's poll loop publishes bounded aggregate attributes
    # (child_poll_count/child_poll_ms_total, backoff_sleep_count/
    # backoff_sleep_ms_total) instead, because one segment per probe grows
    # the operation's segment tree without bound over a long batch.

    MODAL_JOB_METADATA_READ = "modal_job_metadata_read"

    SIMULATION_OUTPUT_BUILD = "simulation_output_build"
    SIMULATION_OUTPUT_MODEL_DUMP = "simulation_output_model_dump"
    ECONOMIC_IMPACT_ANALYSIS = "economic_impact_analysis"
    OUTPUT_BUDGETARY_IMPACT = "output_budgetary_impact"
    OUTPUT_DETAILED_BUDGET = "output_detailed_budget"
    OUTPUT_DECILE = "output_decile"
    OUTPUT_INEQUALITY = "output_inequality"
    OUTPUT_POVERTY = "output_poverty"
    OUTPUT_INTRA_DECILE = "output_intra_decile"
    OUTPUT_WEALTH_DECILE = "output_wealth_decile"
    OUTPUT_INTRA_WEALTH_DECILE = "output_intra_wealth_decile"
    OUTPUT_LABOR_SUPPLY = "output_labor_supply"
    OUTPUT_CONGRESSIONAL_DISTRICT = "output_congressional_district"
    OUTPUT_UK_CONSTITUENCY = "output_uk_constituency"
    OUTPUT_UK_LOCAL_AUTHORITY = "output_uk_local_authority"
    OUTPUT_CLIFF = "output_cliff"
    OUTPUT_MODEL_VERSION = "output_model_version"
    OUTPUT_DATA_VERSION = "output_data_version"


def configure_process_observability(
    *,
    platform: str,
    service_role: str,
    runtime_role: str | None = None,
    modal_app_name: str | None = None,
    modal_function_name: str | None = None,
) -> None:
    os.environ["OBSERVABILITY_PLATFORM"] = platform
    os.environ["OBSERVABILITY_SERVICE_ROLE"] = service_role
    os.environ["OBSERVABILITY_RUNTIME_ROLE"] = runtime_role or service_role
    if modal_app_name:
        os.environ["OBSERVABILITY_MODAL_APP_NAME"] = modal_app_name
    if modal_function_name:
        os.environ["OBSERVABILITY_MODAL_FUNCTION_NAME"] = modal_function_name


def init_simulation_observability(
    app: FastAPI,
    *,
    service_role: str = "api",
) -> ObservabilityRuntime:
    service_role = _service_role(service_role)
    platform = _platform()
    config = _config(service_role=service_role, platform=platform)
    return init_fastapi_observability(
        app,
        config=config,
        runtime=ObservabilityRuntime(config, segment_registry=SegmentName),
        service_name=SERVICE_NAME,
        service_role=service_role,
        span_prefix=SPAN_PREFIX,
        segment_registry=SegmentName,
        static_attributes=_metadata(service_role, platform),
    )


def init_process_observability(
    *,
    service_role: str,
) -> ObservabilityRuntime:
    service_role = _service_role(service_role)
    platform = _platform()
    config = _config(service_role=service_role, platform=platform)
    runtime = ObservabilityRuntime(config, segment_registry=SegmentName)
    runtime.configure()
    set_observability_runtime(runtime)
    return runtime


def process_static_attributes(*, service_role: str) -> dict[str, Any]:
    """Static identity attributes for non-FastAPI (worker) operations.

    The FastAPI adapter applies these per request via ``static_attributes``;
    plain-process runtimes have no equivalent hook, so worker entrypoints must
    merge this dict into their ``operation(...)`` attributes explicitly —
    otherwise operation logs carry no platform/Modal identity.
    """
    return _metadata(_service_role(service_role), _platform())


def logfire_replacement_attributes() -> dict[str, str]:
    return {
        "logfire_status": LOGFIRE_STATUS,
        "logfire_replacement_candidate": LOGFIRE_REPLACEMENT_CANDIDATE,
    }


def _config(
    *,
    service_role: str,
    platform: str,
) -> ObservabilityConfig:
    config = ObservabilityConfig.from_env(
        service_name=SERVICE_NAME,
        service_role=service_role,
        span_prefix=SPAN_PREFIX,
        extra_metric_attribute_keys=SIMULATION_METRIC_ATTRIBUTE_KEYS,
        default_log_destinations=LOG_DESTINATIONS,
    )
    return replace(
        config,
        environment=_environment(),
        log_destinations=LOG_DESTINATIONS,
        otel_enabled=False,
        google_cloud_project=None,
    )


def _environment() -> str:
    return (
        os.getenv("OBSERVABILITY_ENVIRONMENT")
        or os.getenv("MODAL_ENVIRONMENT")
        or os.getenv("DEPLOYMENT_ENVIRONMENT")
        or os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or "local"
    )


def _service_role(default: str) -> str:
    return (
        os.getenv("OBSERVABILITY_SERVICE_ROLE")
        or os.getenv("OBSERVABILITY_RUNTIME_ROLE")
        or default
    )


def _platform() -> str:
    configured = os.getenv("OBSERVABILITY_PLATFORM")
    if configured:
        return configured
    if os.getenv("MODAL_ENVIRONMENT") or os.getenv("MODAL_TASK_ID"):
        return "modal"
    return "local"


def _metadata(service_role: str, platform: str) -> dict[str, Any]:
    values = {
        "platform": platform,
        "runtime_role": os.getenv("OBSERVABILITY_RUNTIME_ROLE") or service_role,
        "modal_environment": os.getenv("MODAL_ENVIRONMENT"),
        "modal_app_name": os.getenv("OBSERVABILITY_MODAL_APP_NAME"),
        "modal_function_name": os.getenv("OBSERVABILITY_MODAL_FUNCTION_NAME"),
        **logfire_replacement_attributes(),
    }
    return {key: value for key, value in values.items() if value}


set_observability_runtime(ObservabilityRuntime.disabled())
