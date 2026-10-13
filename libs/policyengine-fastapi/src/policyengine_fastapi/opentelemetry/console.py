"""Console OpenTelemetry exporters for local debugging."""

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import (
    Resource,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)

from policyengine_fastapi.exit import exit


# Configure opentelemetry
# 1. to include python logs and
# 2. export to console (for demo purposes)
def export_ot_to_console(resource: Resource) -> None:
    """Export OpenTelemetry traces and metrics to the console, for desktop debugging.

    Installs global tracer and meter providers for ``resource`` and registers an
    ``exit`` callback that flushes both at shutdown.
    """
    traceProvider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(ConsoleSpanExporter())
    traceProvider.add_span_processor(processor)
    trace.set_tracer_provider(traceProvider)

    reader = PeriodicExportingMetricReader(ConsoleMetricExporter())
    meterProvider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meterProvider)

    @exit()
    def flush_ot() -> None:
        traceProvider.force_flush()
        meterProvider.force_flush()
