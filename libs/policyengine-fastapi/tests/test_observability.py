from datetime import datetime, UTC

from policyengine_fastapi.observability import (
    NoOpObservability,
    ObservabilityConfig,
    SimulationCompositeTraceResponse,
    SimulationLifecycleEvent,
    SimulationRunSummary,
    SimulationStage,
    SimulationTimelineEntry,
    TracerArtifactManifest,
    TracerCaptureMode,
    VersionStageMetricResponse,
    build_observability,
    generate_observability_id,
    get_observability,
    parse_header_value_pairs,
    stable_config_hash,
)
from pydantic import ValidationError


def test_parse_header_value_pairs__supports_commas_and_newlines():
    raw = "Authorization=Bearer abc,\nX-Scope=production"

    assert parse_header_value_pairs(raw) == {
        "Authorization": "Bearer abc",
        "X-Scope": "production",
    }


def test_parse_header_value_pairs__raises_for_invalid_pair():
    try:
        parse_header_value_pairs("Authorization")
    except ValueError as error:
        assert "key=value" in str(error)
    else:
        raise AssertionError("Expected invalid OTLP header parsing to fail")


def test_observability_config_disabled__returns_disabled_defaults():
    config = ObservabilityConfig.disabled(service_name="test-service")

    assert config.enabled is False
    assert config.service_name == "test-service"
    assert config.otlp_headers == {}
    assert config.tracer_capture_mode == TracerCaptureMode.DISABLED


def test_correlation_helpers__generate_ids_and_stable_hashes():
    observability_id = generate_observability_id()
    left = stable_config_hash({"b": 2, "a": 1})
    right = stable_config_hash({"a": 1, "b": 2})

    assert len(observability_id) == 36
    assert left == right
    assert left.startswith("sha256:")


def test_contract_models__serialize_expected_shapes():
    timestamp = datetime(2026, 4, 9, 20, 0, tzinfo=UTC)

    event = SimulationLifecycleEvent(
        event_name="simulation.stage.completed",
        stage=SimulationStage.WORKER_SIMULATION_CONSTRUCTED,
        status="ok",
        timestamp=timestamp,
        service="policyengine-simulation-worker",
        observability_id="run-123",
    )
    manifest = TracerArtifactManifest(
        observability_id="run-123",
        scenario="baseline",
        capture_mode=TracerCaptureMode.THRESHOLD,
        artifact_format="policyengine.flat_trace.v1",
        storage_uri="gs://bucket/run-123/trace.json.gz",
        generated_at=timestamp,
    )
    response = SimulationCompositeTraceResponse(
        run=SimulationRunSummary(observability_id="run-123", status="complete"),
        timeline=[
            SimulationTimelineEntry(
                stage=SimulationStage.REQUEST_ACCEPTED,
                started_at=timestamp,
                ended_at=timestamp,
                duration_seconds=0.0,
                service="policyengine-api",
            )
        ],
        tracer={"baseline": {"manifest": manifest.model_dump(mode="json")}},
    )
    version_metrics = VersionStageMetricResponse(
        country="us",
        window={"from": timestamp, "to": timestamp},
    )

    dumped_event = event.model_dump(mode="json")
    dumped_response = response.model_dump(mode="json")
    dumped_version_metrics = version_metrics.model_dump(mode="json")

    assert dumped_event["stage"] == "worker.simulation.constructed"
    assert dumped_response["timeline"][0]["stage"] == "request.accepted"
    assert (
        dumped_response["tracer"]["baseline"]["manifest"]["capture_mode"] == "threshold"
    )
    assert dumped_version_metrics["versions"] == []


def test_contract_models__reject_extra_fields():
    try:
        SimulationLifecycleEvent(
            event_name="simulation.stage.completed",
            stage=SimulationStage.WORKER_COMPLETED,
            status="ok",
            timestamp=datetime(2026, 4, 9, 20, 0, tzinfo=UTC),
            service="policyengine-simulation-worker",
            observability_id="run-789",
            unexpected=True,
        )
    except ValidationError as error:
        assert "unexpected" in str(error)
    else:
        raise AssertionError("Expected extra field validation to fail")


def test_noop_observability__accepts_calls_without_side_effects():
    observability = NoOpObservability()
    event = SimulationLifecycleEvent(
        event_name="simulation.stage.completed",
        stage=SimulationStage.WORKER_COMPLETED,
        status="ok",
        timestamp=datetime(2026, 4, 9, 20, 0, tzinfo=UTC),
        service="policyengine-simulation-worker",
        observability_id="run-456",
    )

    observability.emit_lifecycle_event(event)
    observability.emit_counter("policyengine.simulation.run.count")
    observability.emit_histogram("policyengine.simulation.run.duration.seconds", 1.23)
    with observability.span("run_simulation") as span:
        span.set_attribute("observability_id", "run-456")
        span.add_event("simulation.completed")
    observability.flush()


def test_observability_provider__returns_noop_for_disabled_defaults():
    observability = build_observability()

    assert isinstance(observability, NoOpObservability)
    assert observability.config == ObservabilityConfig.disabled()


def test_observability_provider__preserves_supplied_config():
    config = ObservabilityConfig(
        enabled=True,
        service_name="simulation-worker",
        tracer_capture_mode=TracerCaptureMode.THRESHOLD,
    )

    built = build_observability(config)
    fetched = get_observability(config)

    assert isinstance(built, NoOpObservability)
    assert built.config == config
    assert isinstance(fetched, NoOpObservability)
    assert fetched.config == config
