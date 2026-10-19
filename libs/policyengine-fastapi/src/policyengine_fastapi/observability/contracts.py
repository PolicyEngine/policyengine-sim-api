"""Pydantic payloads for run lifecycle events, tracer manifests and trace views."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .stages import SimulationStage, TracerCaptureMode


class ObservabilityModel(BaseModel):
    """Observability payload base; rejects unknown fields and stores enum values."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class CorrelatedRunFields(ObservabilityModel):
    """Identifiers and version metadata that tie a record to one simulation run.

    Only ``observability_id`` is required; the rest are filled in as each
    service learns them.
    """

    observability_id: str
    submission_claim_id: str | None = None
    job_id: str | None = None
    trace_id: str | None = None
    request_id: str | None = None
    country: str | None = None
    simulation_kind: str | None = None
    geography_code: str | None = None
    geography_type: str | None = None
    country_package_name: str | None = None
    country_package_version: str | None = None
    policyengine_version: str | None = None
    data_version: str | None = None
    modal_app_name: str | None = None
    config_hash: str | None = None


class SimulationLifecycleEvent(CorrelatedRunFields):
    """One stage transition of a run, as reported by the service that saw it."""

    event_name: str
    stage: SimulationStage
    status: str
    timestamp: datetime
    service: str
    duration_seconds: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class TracerArtifactManifest(CorrelatedRunFields):
    """Where a run's computation-tracer capture is stored, with its size and timings."""

    scenario: str
    capture_mode: TracerCaptureMode
    artifact_format: str
    storage_uri: str
    summary_uri: str | None = None
    node_count: int = 0
    root_count: int = 0
    max_depth: int = 0
    total_calculation_time_seconds: float = 0.0
    total_formula_time_seconds: float = 0.0
    generated_at: datetime


class SimulationRunSummary(CorrelatedRunFields):
    """Overall status and end-to-end timing of one simulation run."""

    status: str
    requested_at: datetime | None = None
    returned_at: datetime | None = None
    total_duration_seconds: float | None = None


class SimulationTimelineEntry(ObservabilityModel):
    """Interval one service spent in one stage of a run."""

    stage: SimulationStage
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    service: str


class SimulationCompositeTraceResponse(ObservabilityModel):
    """Combined view of a run: summary, stage timeline, spans, logs and tracer data."""

    run: SimulationRunSummary
    timeline: list[SimulationTimelineEntry] = Field(default_factory=list)
    spans: dict[str, Any] = Field(default_factory=dict)
    logs: dict[str, Any] = Field(default_factory=dict)
    tracer: dict[str, Any] = Field(default_factory=dict)


class StageRuntimeSummary(ObservabilityModel):
    """Mean, median and p95 stage duration in seconds; None where unobserved."""

    mean_seconds: float | None = None
    p50_seconds: float | None = None
    p95_seconds: float | None = None


class VersionStageMetrics(ObservabilityModel):
    """Per-stage runtime statistics for runs on one country package version."""

    country_package_version: str
    launch_source: str = "modal_version_registry"
    launched: bool = True
    observed_run_count: int = 0
    stages: dict[str, StageRuntimeSummary] = Field(default_factory=dict)


class VersionStageMetricResponse(ObservabilityModel):
    """Per-version stage runtime metrics for one country over a time window."""

    country: str
    window: dict[str, datetime]
    versions: list[VersionStageMetrics] = Field(default_factory=list)
