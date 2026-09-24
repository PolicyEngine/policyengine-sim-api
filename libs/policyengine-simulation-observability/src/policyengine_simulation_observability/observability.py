from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

from fastapi import FastAPI
from policyengine_observability import (
    DeploymentIdentity,
    GoogleCloudLogDestination,
    GoogleCloudLogFormatter,
    LogDestinationStrategy,
    LoggingConfig,
    ObservabilityConfig,
    ObservabilityRuntime,
    ServiceIdentity,
    StdoutLogDestination,
    configure,
    instrument_fastapi,
)
from starlette.datastructures import Headers

from policyengine_simulation_observability.identifiers import (
    OBSERVABILITY_ID_HEADER,
    normalize_observability_id,
)

Platform = Literal["google_cloud_run", "modal", "local", "other"]

MODAL_IMAGE_ENV_NAMES = (
    "OBSERVABILITY_SERVICE_NAMESPACE",
    "OBSERVABILITY_TRACE_PROJECT_ID",
    "OBSERVABILITY_LOGGING_PROJECT_ID",
    "OBSERVABILITY_LOG_NAME",
    "OBSERVABILITY_GOOGLE_WORKLOAD_IDENTITY_PROVIDER",
    "OBSERVABILITY_GOOGLE_SERVICE_ACCOUNT_EMAIL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "POLICYENGINE_OTEL_GOOGLE_AUDIENCE",
)

SIMULATION_ATTRIBUTE_KEYS = frozenset(
    {
        "ack_value_present",
        "backend",
        "backoff_sleep_count",
        "backoff_sleep_ms_total",
        "baseline_artifact",
        "batch_job_id",
        "capture_mode",
        "child_poll_count",
        "child_poll_ms_total",
        "config_hash",
        "correlation_id",
        "country",
        "data_version",
        "dataset",
        "elapsed_ms",
        "error_type",
        "evaluation_id",
        "execution_mode",
        "function_call_id",
        "geography_code",
        "geography_type",
        "include_cliffs",
        "job_id",
        "job_state",
        "method",
        "modal_app_name",
        "modal_application",
        "modal_environment",
        "modal_function_name",
        "model_version",
        "national_execution",
        "parent_call_not_found",
        "partition_uncovered_regions",
        "policyengine_version",
        "submission_claim_id",
        "production_identity",
        "reason",
        "region",
        "region_group",
        "requested_version",
        "request_id",
        "runner_name",
        "resolved_app_name",
        "resolved_version",
        "route",
        "observability_id",
        "coordinator_invocation_id",
        "scope",
        "segmented",
        "segmented_group_count",
        "segmented_poll_count",
        "simulation_kind",
        "simulation_role",
        "simulation_year",
        "status_code",
        "time_period",
        "version",
        "worker_version",
    }
)

DISPATCH_ATTRIBUTE_KEYS = frozenset({"job_id", "observability_id", "simulation_id"})


class _RuntimeObservabilityIdMiddleware:
    """Attach the transport identifier after request tracing has started."""

    def __init__(self, app: Any, *, runtime: ObservabilityRuntime) -> None:
        self.app = app
        self.runtime = runtime

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") == "http":
            observability_id = normalize_observability_id(
                Headers(scope=scope).get(OBSERVABILITY_ID_HEADER)
            )
            if observability_id is not None:
                try:
                    self.runtime.set_context(observability_id=observability_id)
                except Exception:  # noqa: BLE001, S110 - telemetry is non-fatal
                    pass
        await self.app(scope, receive, send)


def modal_image_environment() -> dict[str, str]:
    """Return deployment-provided settings to bake into a Modal image."""

    configured = {
        name: value
        for name in MODAL_IMAGE_ENV_NAMES
        if (value := os.getenv(name, "").strip())
    }
    return {
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_TRACES_SAMPLER_ARG": "1.0",
        **configured,
    }


def build_runtime(
    *,
    service_name: str,
    service_role: str,
    platform: Platform,
    environment: str,
    service_version: str | None = None,
) -> ObservabilityRuntime:
    """Create one explicitly owned simulation observability runtime."""

    trace_project = os.getenv("OBSERVABILITY_TRACE_PROJECT_ID", "").strip()
    logging_project = os.getenv("OBSERVABILITY_LOGGING_PROJECT_ID", "").strip()
    log_name = os.getenv("OBSERVABILITY_LOG_NAME", "").strip()
    destinations: list[LogDestinationStrategy] = [
        StdoutLogDestination(
            formatter=(
                GoogleCloudLogFormatter(trace_project) if trace_project else None
            )
        )
    ]
    if logging_project or log_name:
        destinations.append(
            GoogleCloudLogDestination(
                project_id=logging_project,
                log_name=log_name,
            )
        )
    config = ObservabilityConfig.from_env(
        service=ServiceIdentity(
            name=service_name,
            namespace=os.getenv(
                "OBSERVABILITY_SERVICE_NAMESPACE",
                "policyengine.api-v1",
            ),
            version=service_version or _service_version(),
            role=service_role,
        ),
        deployment=DeploymentIdentity(
            environment=environment,
            platform=platform,
            region=os.getenv("GOOGLE_CLOUD_REGION") or os.getenv("CLOUD_RUN_REGION"),
            instance_id=os.getenv("K_REVISION") or os.getenv("MODAL_TASK_ID"),
        ),
        logging=LoggingConfig(
            destinations=tuple(destinations),
            capture_standard_library=True,
        ),
        application_attribute_keys=SIMULATION_ATTRIBUTE_KEYS,
        dispatch_attribute_keys=DISPATCH_ATTRIBUTE_KEYS,
    )
    config = replace(
        config,
        otel=replace(config.otel, sampling_ratio=1.0),
    )
    return configure(config)


def init_simulation_observability(
    app: FastAPI,
    *,
    service_name: str,
    service_role: str,
    platform: Platform,
    environment: str,
    service_version: str | None = None,
) -> ObservabilityRuntime:
    runtime = build_runtime(
        service_name=service_name,
        service_role=service_role,
        platform=platform,
        environment=environment,
        service_version=service_version,
    )
    # FastAPI applies the most recently registered middleware first. Install
    # this context layer before the lifecycle integration so it executes after
    # begin_request has created the request-local runtime state. Application
    # correlation middleware may then be registered outside both layers and
    # normalize the header before either observability layer reads it.
    app.add_middleware(_RuntimeObservabilityIdMiddleware, runtime=runtime)
    return instrument_fastapi(app, runtime)


def init_process_observability(
    *,
    service_name: str,
    service_role: str,
    platform: Platform,
    environment: str,
    service_version: str | None = None,
) -> ObservabilityRuntime:
    return build_runtime(
        service_name=service_name,
        service_role=service_role,
        platform=platform,
        environment=environment,
        service_version=service_version,
    )


def _service_version() -> str:
    for name in (
        "POLICYENGINE_SERVICE_VERSION",
        "K_REVISION",
        "POLICYENGINE_VERSION",
    ):
        value = os.getenv(name)
        if value:
            return value
    try:
        return version("policyengine-simulation-observability")
    except PackageNotFoundError:
        return "0.1.0"
