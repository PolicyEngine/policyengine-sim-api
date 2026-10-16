"""Google Cloud exporters for OpenTelemetry, and JSON logs shaped for Cloud Logging."""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from policyengine_fastapi.exit import exit

log = logging.getLogger(__name__)

# see https://cloud.google.com/trace/docs/setup/python-ot


# except, google lies. For the trace to work it actually needs to be in the format
# projects/policyengine-448017/traces/50d5653a403a853d9d2720690e19c51e
# but thats _NOT_ what's in the header they pass the app (that's just the trace id)
# so the otelTraceID is just the ID by iself.
# so we have to add it


class AddGcpProjectToTraceFilter(logging.Filter):
    """Logging filter that qualifies trace IDs as ``projects/<project>/traces/<id>``."""

    def __init__(self, project: str) -> None:
        """Remember the GCP project used to qualify trace IDs."""
        super().__init__()
        self.project = project

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        """Qualify the record's ``otelTraceID``, if set, then apply the base filter."""
        if not hasattr(record, "otelTraceID"):
            return super().filter(record)
        record.otelTraceID = f"projects/{self.project}/traces/{record.otelTraceID}"  # type: ignore
        return super().filter(record)


def _get_project_id() -> str:
    return (
        _get_project_id_from_metadata()
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or "NO_PROJECT_SPECIFIED"
    )


def _get_project_id_from_metadata() -> str | None:
    from urllib import request

    url = "http://metadata.google.internal/computeMetadata/v1/project/project-id"
    req = request.Request(url)
    req.add_header("Metadata-Flavor", "Google")
    try:
        return request.urlopen(req).read().decode()
    except Exception as err:  # noqa: BLE001  # best-effort probe: any failure falls back to GOOGLE_CLOUD_PROJECT
        log.info(
            f"Unable to get the google project id from a local metadata service: {err}"
        )
    return None


class GCPLoggingInstrumentor:
    """Logging setup that emits JSON logs in the shape Google Cloud Logging expects.

    Configures the standard OpenTelemetry logging instrumentor and renames level,
    timestamp and the trace and span IDs to the Cloud Logging fields, so log
    lines link to their traces.
    """

    def __init__(self, project_id: str | None = None) -> None:
        """Use ``project_id``, else look the project up.

        The lookup asks the GCE metadata server, then ``GOOGLE_CLOUD_PROJECT``.
        """
        self.project_id = project_id or _get_project_id()

    def instrument(self) -> None:
        """Send JSON logs to stderr and add OpenTelemetry trace IDs to log records.

        Uses ``logging.basicConfig``, so the handler is not installed if the root
        logger already has handlers.
        """
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        from pythonjsonlogger.json import JsonFormatter

        logHandler = logging.StreamHandler()
        formatter = JsonFormatter(
            "%(asctime)s %(levelname)s %(message)s %(otelTraceID)s %(otelSpanID)s %(otelTraceSampled)s",
            rename_fields={
                "levelname": "severity",
                "asctime": "timestamp",
                "otelTraceID": "logging.googleapis.com/trace",
                "otelSpanID": "logging.googleapis.com/spanId",
                "otelTraceSampled": "logging.googleapis.com/trace_sampled",
            },
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )

        logHandler.setFormatter(formatter)
        logHandler.addFilter(AddGcpProjectToTraceFilter(self.project_id))

        logging.basicConfig(
            level=logging.INFO,
            handlers=[logHandler],
        )
        LoggingInstrumentor().instrument()


def export_ot_to_gcp(resource: Resource) -> None:
    """Export traces and metrics straight to Cloud Trace and Cloud Monitoring.

    Meant for code running in Google Cloud. Installs global tracer and meter
    providers for ``resource`` and registers an ``exit`` callback that flushes
    both at shutdown.
    """
    from opentelemetry.exporter.cloud_monitoring import (
        CloudMonitoringMetricsExporter,
    )
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

    traceProvider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(CloudTraceSpanExporter())
    traceProvider.add_span_processor(processor)
    trace.set_tracer_provider(traceProvider)

    reader = PeriodicExportingMetricReader(CloudMonitoringMetricsExporter())
    meterProvider = MeterProvider(metric_readers=[reader], resource=resource)
    metrics.set_meter_provider(meterProvider)

    @exit()
    def flush_ot() -> None:
        log.info("Flushing traces and metrics before shutdown")
        traceProvider.force_flush()
        meterProvider.force_flush()
        log.info("All metics and traces flushed.")
