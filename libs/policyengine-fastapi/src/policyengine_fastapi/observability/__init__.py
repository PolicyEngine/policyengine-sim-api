"""
Shared observability contracts and utilities for the PolicyEngine simulation API services.

This stays in `policyengine-fastapi` for now because only one active service
needs it and the main value today is shared contract stability, not a separate
distribution boundary. Extract it to a dedicated package only when a second
service has materially different runtime or release needs.
"""

from .config import (
    ObservabilityConfig as ObservabilityConfig,
    parse_header_value_pairs as parse_header_value_pairs,
)
from .contracts import (
    SimulationCompositeTraceResponse as SimulationCompositeTraceResponse,
    SimulationLifecycleEvent as SimulationLifecycleEvent,
    SimulationRunSummary as SimulationRunSummary,
    SimulationTelemetryEnvelope as SimulationTelemetryEnvelope,
    SimulationTimelineEntry as SimulationTimelineEntry,
    TracerArtifactManifest as TracerArtifactManifest,
    VersionStageMetricResponse as VersionStageMetricResponse,
)
from .correlation import (
    generate_run_id as generate_run_id,
    stable_config_hash as stable_config_hash,
)
from .emitters import (
    Observability as Observability,
    NoOpObservability as NoOpObservability,
    NoOpSpan as NoOpSpan,
)
from .provider import (
    build_observability as build_observability,
    get_observability as get_observability,
)
from .stages import (
    SimulationStage as SimulationStage,
    TracerCaptureMode as TracerCaptureMode,
)
