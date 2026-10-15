"""
Shared observability contracts and utilities for the PolicyEngine simulation API services.

This stays in `policyengine-fastapi` for now because only one active service
needs it and the main value today is shared contract stability, not a separate
distribution boundary. Extract it to a dedicated package only when a second
service has materially different runtime or release needs.
"""

from .config import (
    ObservabilityConfig as ObservabilityConfig,
)
from .config import (
    parse_header_value_pairs as parse_header_value_pairs,
)
from .contracts import (
    SimulationCompositeTraceResponse as SimulationCompositeTraceResponse,
)
from .contracts import (
    SimulationLifecycleEvent as SimulationLifecycleEvent,
)
from .contracts import (
    SimulationRunSummary as SimulationRunSummary,
)
from .contracts import (
    SimulationTimelineEntry as SimulationTimelineEntry,
)
from .contracts import (
    TracerArtifactManifest as TracerArtifactManifest,
)
from .contracts import (
    VersionStageMetricResponse as VersionStageMetricResponse,
)
from .correlation import (
    generate_observability_id as generate_observability_id,
)
from .correlation import (
    stable_config_hash as stable_config_hash,
)
from .emitters import (
    NoOpObservability as NoOpObservability,
)
from .emitters import (
    NoOpSpan as NoOpSpan,
)
from .emitters import (
    Observability as Observability,
)
from .provider import (
    build_observability as build_observability,
)
from .provider import (
    get_observability as get_observability,
)
from .stages import (
    SimulationStage as SimulationStage,
)
from .stages import (
    TracerCaptureMode as TracerCaptureMode,
)
