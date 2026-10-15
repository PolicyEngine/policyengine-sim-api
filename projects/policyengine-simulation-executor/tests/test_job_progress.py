"""Tests for job stage reporting in the simulation worker."""

from __future__ import annotations

import importlib
import logging
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import NoOpObservabilityRuntime
from fixtures.fake_modal import install_fake_modal
from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from fixtures.spm_doubles import installed_spm_selection, spm_capable_simulation
from policyengine_simulation_contract.job_progress import (
    JobExecution,
    JobProgressRecord,
    JobSegmentProgress,
    JobStage,
    job_progress_fields,
)
from policyengine_simulation_executor import simulation_runtime as sr
from policyengine_simulation_executor.job_progress import (
    NULL_JOB_PROGRESS,
    StoredJobProgressReporter,
)
from policyengine_simulation_executor.release_bundle import (
    get_bundled_package_version,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)
from policyengine_simulation_executor.simulation_runtime import (
    RegionResolution,
    _run_simulation_impl_core,
)
from src.modal import segmented_national as sn

T0 = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


class _Clock:
    def __init__(self):
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _RecordingWriter:
    def __init__(self):
        self.writes: list[tuple[str, JobProgressRecord]] = []

    def __call__(self, job_id: str, record: JobProgressRecord) -> None:
        self.writes.append((job_id, record))

    @property
    def last(self) -> JobProgressRecord:
        return self.writes[-1][1]


class _RecordingReporter:
    """Records reporter calls in order."""

    def __init__(self):
        self.calls: list[tuple] = []

    def start(self) -> None:
        self.calls.append(("start",))

    def set_execution(self, execution: JobExecution) -> None:
        self.calls.append(("set_execution", execution))

    def enter(self, stage: JobStage) -> None:
        self.calls.append(("enter", stage))

    def start_segments(self, total: int) -> None:
        self.calls.append(("start_segments", total))

    def segment_completed(self) -> None:
        self.calls.append(("segment_completed",))

    def finish(self) -> None:
        self.calls.append(("finish",))

    def interrupt(self) -> None:
        self.calls.append(("interrupt",))

    def stage_durations(self) -> dict[JobStage, float]:
        return {}


def _reporter():
    clock = _Clock()
    writer = _RecordingWriter()
    reporter = StoredJobProgressReporter("fc-job-1", write=writer, clock=clock)
    return reporter, writer, clock


class TestStoredJobProgressReporter:
    def test__monolithic_lifecycle_records_each_stage_duration(self):
        reporter, writer, clock = _reporter()

        reporter.start()
        clock.advance(10)
        reporter.enter(JobStage.BASELINE)
        clock.advance(20)
        reporter.enter(JobStage.REFORM)
        clock.advance(30)
        reporter.enter(JobStage.OUTPUTS)
        clock.advance(5)
        reporter.finish()

        assert [record.stage for _, record in writer.writes] == [
            JobStage.LOADING_DATA,
            JobStage.BASELINE,
            JobStage.REFORM,
            JobStage.OUTPUTS,
            JobStage.OUTPUTS,
        ]
        assert {job_id for job_id, _ in writer.writes} == {"fc-job-1"}
        final = writer.last
        assert final.started_at == T0
        assert final.stage_started_at == T0 + timedelta(seconds=60)
        assert final.finished_at == T0 + timedelta(seconds=65)
        assert final.stage_durations == {
            JobStage.LOADING_DATA: 10.0,
            JobStage.BASELINE: 20.0,
            JobStage.REFORM: 30.0,
            JobStage.OUTPUTS: 5.0,
        }

    def test__written_records_are_snapshots(self):
        reporter, writer, clock = _reporter()

        reporter.start()
        clock.advance(10)
        reporter.enter(JobStage.BASELINE)

        assert writer.writes[0][1].stage is JobStage.LOADING_DATA
        assert writer.writes[0][1].stage_durations == {}

    def test__entering_the_current_stage_writes_nothing(self):
        reporter, writer, _ = _reporter()

        reporter.start()
        reporter.enter(JobStage.LOADING_DATA)
        reporter.start()

        assert len(writer.writes) == 1

    def test__enter_before_start_starts_the_record(self):
        reporter, writer, _ = _reporter()

        reporter.enter(JobStage.BASELINE)

        assert [record.stage for _, record in writer.writes] == [
            JobStage.LOADING_DATA,
            JobStage.BASELINE,
        ]

    def test__segments_count_up_to_their_total(self):
        reporter, writer, _ = _reporter()

        reporter.start()
        reporter.start_segments(2)
        reporter.segment_completed()
        reporter.segment_completed()
        reporter.segment_completed()

        assert writer.last.stage is JobStage.SIMULATING
        assert writer.last.segments == JobSegmentProgress(completed=2, total=2)
        assert len(writer.writes) == 5

    def test__execution_path_is_recorded_once(self):
        reporter, writer, _ = _reporter()

        reporter.start()
        reporter.set_execution(JobExecution.MONOLITHIC)
        reporter.set_execution(JobExecution.MONOLITHIC)

        assert [record.execution for _, record in writer.writes] == [
            None,
            JobExecution.MONOLITHIC,
        ]
        assert writer.last.stage is JobStage.LOADING_DATA

    def test__stage_durations_are_a_copy_of_ended_stages(self):
        reporter, _, clock = _reporter()

        assert reporter.stage_durations() == {}
        reporter.start()
        clock.advance(4)
        reporter.enter(JobStage.REFORM)
        durations = reporter.stage_durations()
        durations[JobStage.REFORM] = 1.0

        assert reporter.stage_durations() == {JobStage.LOADING_DATA: 4.0}

    def test__segment_completion_without_segments_writes_nothing(self):
        reporter, writer, _ = _reporter()

        reporter.start()
        reporter.segment_completed()

        assert len(writer.writes) == 1

    def test__nothing_changes_after_finish(self):
        reporter, writer, clock = _reporter()

        reporter.start()
        clock.advance(3)
        reporter.finish()
        reporter.finish()
        reporter.enter(JobStage.REFORM)
        reporter.set_execution(JobExecution.SEGMENTED)
        reporter.segment_completed()

        assert len(writer.writes) == 2
        assert writer.last.stage is JobStage.LOADING_DATA
        assert writer.last.stage_durations == {JobStage.LOADING_DATA: 3.0}

    def test__finish_without_start_writes_nothing(self):
        reporter, writer, _ = _reporter()

        reporter.finish()

        assert writer.writes == []

    def test__write_failures_are_logged_once_and_never_raised(self, caplog):
        calls = []

        def failing_write(_job_id, record):
            calls.append(record.stage)
            msg = "modal unavailable"
            raise ConnectionError(msg)

        reporter = StoredJobProgressReporter("fc-job-1", write=failing_write)

        with caplog.at_level(logging.WARNING):
            reporter.start()
            reporter.enter(JobStage.BASELINE)
            reporter.finish()

        assert calls == [JobStage.LOADING_DATA, JobStage.BASELINE, JobStage.BASELINE]
        warnings = [
            record for record in caplog.records if "job progress" in record.message
        ]
        assert len(warnings) == 1
        assert "ConnectionError" in warnings[0].message
        assert "modal unavailable" not in warnings[0].message


def _earlier_attempt(**overrides) -> JobProgressRecord:
    values = {
        "stage": JobStage.REFORM,
        "execution": JobExecution.MONOLITHIC,
        "started_at": T0 - timedelta(seconds=100),
        "stage_started_at": T0 - timedelta(seconds=30),
        "stage_durations": {JobStage.LOADING_DATA: 40.0, JobStage.BASELINE: 30.0},
    }
    return JobProgressRecord(**{**values, **overrides})


def _restarted_reporter(earlier):
    clock = _Clock()
    writer = _RecordingWriter()
    reporter = StoredJobProgressReporter(
        "fc-job-1", write=writer, read=lambda _job_id: earlier, clock=clock
    )
    return reporter, writer, clock


def _running_progress(record: JobProgressRecord) -> int:
    return job_progress_fields(
        status="running", submitted_at=None, record=record, reports_stages=True
    )["progress"]


class TestRestartedAttempt:
    """Modal can run the same call again after a preemption (same call id)."""

    def test__restart_continues_the_earlier_record(self):
        reporter, writer, _ = _restarted_reporter(_earlier_attempt())

        reporter.start()

        first = writer.writes[0][1]
        assert first.started_at == T0 - timedelta(seconds=100)
        assert first.attempts == 2
        assert first.execution is JobExecution.MONOLITHIC
        assert first.stage_durations == {
            JobStage.LOADING_DATA: 40.0,
            JobStage.BASELINE: 30.0,
        }
        # Clients still see the stage the earlier attempt reached.
        assert first.stage is JobStage.REFORM
        assert first.stage_started_at == T0 - timedelta(seconds=30)

    def test__restart_never_moves_progress_backwards(self):
        reporter, writer, clock = _restarted_reporter(_earlier_attempt())

        reporter.start()
        clock.advance(10)
        reporter.set_execution(JobExecution.MONOLITHIC)
        reporter.enter(JobStage.BASELINE)
        clock.advance(20)
        reporter.enter(JobStage.REFORM)
        clock.advance(30)
        reporter.enter(JobStage.OUTPUTS)

        progress = [_running_progress(record) for _, record in writer.writes]
        assert progress == sorted(progress)
        assert progress[0] == 67
        assert progress[-1] == 97
        # Held at the earlier attempt's reform stage, then this attempt's own.
        reform_starts = [
            record.stage_started_at
            for _, record in writer.writes
            if record.stage is JobStage.REFORM
        ]
        assert reform_starts[0] == T0 - timedelta(seconds=30)
        assert reform_starts[-1] == T0 + timedelta(seconds=30)

    def test__durations_add_up_over_attempts(self):
        reporter, writer, clock = _restarted_reporter(_earlier_attempt())

        reporter.start()
        clock.advance(5)
        reporter.enter(JobStage.BASELINE)
        clock.advance(10)
        reporter.finish()

        assert writer.last.stage_durations == {
            JobStage.LOADING_DATA: 45.0,
            JobStage.BASELINE: 40.0,
        }
        assert reporter.attempts() == 2

    def test__failure_reports_the_stage_this_attempt_stopped_in(self):
        reporter, writer, clock = _restarted_reporter(_earlier_attempt())

        reporter.start()
        clock.advance(5)
        reporter.finish()

        assert writer.last.stage is JobStage.LOADING_DATA
        assert writer.last.finished_at == T0 + timedelta(seconds=5)

    def test__a_finished_earlier_attempt_is_continued_too(self):
        # An attempt stopped by something other than the SIGINT the worker
        # handles (or an older worker) can leave a finished record behind.
        earlier = _earlier_attempt(finished_at=T0 - timedelta(seconds=1))
        reporter, writer, _ = _restarted_reporter(earlier)

        reporter.start()

        assert writer.last.attempts == 2
        assert writer.last.finished_at is None
        assert writer.last.stage is JobStage.REFORM

    def test__preemption_leaves_the_record_running_for_the_next_attempt(self):
        reporter, writer, clock = _restarted_reporter(_earlier_attempt())

        reporter.start()
        clock.advance(5)
        reporter.interrupt()
        reporter.finish()

        # start and interrupt write; finish writes nothing after interrupt.
        assert len(writer.writes) == 2
        assert writer.last.finished_at is None
        # The earlier attempt's stage stays held for the next attempt.
        assert writer.last.stage is JobStage.REFORM
        assert writer.last.stage_started_at == T0 - timedelta(seconds=30)
        # This attempt's time still counts.
        assert writer.last.stage_durations[JobStage.LOADING_DATA] == 45.0
        assert writer.last.attempts == 2

    def test__repeated_preemptions_never_move_progress_backwards(self):
        store: dict[str, JobProgressRecord] = {}
        published: list[JobProgressRecord] = []

        def write(job_id: str, record: JobProgressRecord) -> None:
            store[job_id] = record
            published.append(record)

        def attempt() -> tuple[StoredJobProgressReporter, _Clock]:
            clock = _Clock()
            reporter = StoredJobProgressReporter(
                "fc-job-1", write=write, read=store.get, clock=clock
            )
            reporter.start()
            return reporter, clock

        # Attempt 1 reaches the reform, then is preempted.
        first, clock = attempt()
        first.set_execution(JobExecution.MONOLITHIC)
        first.enter(JobStage.BASELINE)
        first.enter(JobStage.REFORM)
        first.interrupt()
        first.finish()
        # Attempt 2 is preempted while still loading data.
        second, clock = attempt()
        clock.advance(5)
        second.interrupt()
        second.finish()
        # Attempt 3 still starts from the reform.
        attempt()

        progress = [_running_progress(record) for record in published]
        assert progress == sorted(progress)
        assert store["fc-job-1"].stage is JobStage.REFORM
        assert store["fc-job-1"].attempts == 3
        assert all(record.finished_at is None for record in published)

    def test__interrupt_after_finish_writes_nothing(self):
        reporter, writer, _ = _reporter()
        reporter.start()
        reporter.finish()
        writes = len(writer.writes)

        reporter.interrupt()

        assert len(writer.writes) == writes
        assert writer.last.finished_at is not None

    def test__segmented_restart_is_held_until_simulating(self):
        earlier = _earlier_attempt(
            stage=JobStage.SIMULATING,
            execution=JobExecution.SEGMENTED,
            segments=JobSegmentProgress(completed=15, total=20),
        )
        reporter, writer, _ = _restarted_reporter(earlier)

        reporter.start()
        reporter.set_execution(JobExecution.SEGMENTED)
        reporter.start_segments(20)

        # The earlier record already says segmented, so set_execution writes
        # nothing; every write shows simulating.
        assert [record.stage for _, record in writer.writes] == [
            JobStage.SIMULATING,
            JobStage.SIMULATING,
            JobStage.SIMULATING,
        ]
        assert writer.last.segments == JobSegmentProgress(completed=0, total=20)
        progress = [_running_progress(record) for _, record in writer.writes]
        assert progress == sorted(progress)

    def test__unreadable_earlier_record_starts_fresh(self, caplog):
        def failing_read(_job_id):
            msg = "modal unavailable"
            raise ConnectionError(msg)

        writer = _RecordingWriter()
        reporter = StoredJobProgressReporter(
            "fc-job-1", write=writer, read=failing_read, clock=_Clock()
        )

        with caplog.at_level(logging.WARNING):
            reporter.start()

        assert writer.last.attempts == 1
        assert writer.last.started_at == T0
        assert "ConnectionError" in caplog.text


class TestStageReportingSimulation:
    def test__callback_runs_once_before_the_first_ensure(self, monkeypatch):
        from policyengine.core import Simulation
        from policyengine_simulation_executor.stage_reporting_simulation import (
            StageReportingSimulation,
        )

        events = []
        monkeypatch.setattr(Simulation, "ensure", lambda _self: events.append("ensure"))
        simulation = StageReportingSimulation.model_construct()
        simulation.on_first_ensure(lambda: events.append("callback"))

        simulation.ensure()
        simulation.ensure()

        assert events == ["callback", "ensure", "ensure"]

    def test__callback_is_cleared_before_the_calculation_runs(self, monkeypatch):
        from policyengine.core import Simulation
        from policyengine_simulation_executor.stage_reporting_simulation import (
            StageReportingSimulation,
        )

        seen = []
        monkeypatch.setattr(
            Simulation,
            "ensure",
            lambda self: seen.append(self._on_first_ensure),
        )
        simulation = StageReportingSimulation.model_construct()
        simulation.on_first_ensure(lambda: None)

        simulation.ensure()

        assert seen == [None]

    def test__artifact_baseline_reports_its_first_ensure(self):
        from policyengine_simulation_executor.baseline_artifacts import (
            ArtifactBaselineSimulation,
        )
        from policyengine_simulation_executor.stage_reporting_simulation import (
            StageReportingSimulation,
        )

        assert issubclass(ArtifactBaselineSimulation, StageReportingSimulation)


class TestSimulationOutputBuilderStage:
    def _builder(self, analysis, progress):
        country_module = SimpleNamespace(economic_impact_analysis=analysis)
        return SimulationOutputBuilder(
            country="us",
            simulation_params={},
            country_module=country_module,
            dataset=None,
            baseline="baseline",
            reform="reform",
            progress=progress,
        )

    def test__outputs_stage_starts_after_the_impact_analysis(self):
        progress = _RecordingReporter()
        calls = []

        def analysis(_baseline, _reform, *, include_cliff_impacts):  # noqa: ARG001  # keyword-only, as SimulationOutputBuilder passes it
            calls.append(list(progress.calls))
            return "analysis"

        builder = self._builder(analysis, progress)

        assert builder.analysis == "analysis"
        assert builder.analysis == "analysis"
        assert calls == [[]]
        assert progress.calls == [("enter", JobStage.OUTPUTS)]

    def test__failed_analysis_does_not_enter_outputs(self):
        progress = _RecordingReporter()

        def analysis(_baseline, _reform, *, include_cliff_impacts):  # noqa: ARG001  # keyword-only, as SimulationOutputBuilder passes it
            msg = "model failed"
            raise RuntimeError(msg)

        builder = self._builder(analysis, progress)

        with pytest.raises(RuntimeError):
            _ = builder.analysis
        assert progress.calls == []


def test_runtime_reports_baseline_then_reform_when_each_calculation_starts(
    monkeypatch,
):
    selection = installed_spm_selection()
    callbacks = {}

    def simulation_double(role):
        return spm_capable_simulation(
            selection,
            on_first_ensure=lambda callback: callbacks.__setitem__(role, callback),
        )

    simulations = iter([simulation_double("baseline"), simulation_double("reform")])
    progress = _RecordingReporter()
    builders = []

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            builders.append(kwargs)

        def serialize(self):
            # economic_impact_analysis ensures the baseline, then the reform.
            callbacks["baseline"]()
            callbacks["reform"]()
            return dict(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    monkeypatch.setattr(
        sr,
        "_country_module",
        lambda _country: SimpleNamespace(model=SimpleNamespace(version="1.715.2")),
    )
    monkeypatch.setattr(
        sr,
        "_resolve_region",
        lambda **_kwargs: RegionResolution(code="us", dataset_reference=None),
    )
    monkeypatch.setattr(
        sr,
        "_load_dataset",
        lambda _params, *, country_module, selection: object(),  # noqa: ARG005  # keyword-only, as the runtime passes them
    )
    monkeypatch.setattr(
        sr, "_build_simulation", lambda *_args, **_kwargs: next(simulations)
    )
    monkeypatch.setattr(sr, "SimulationOutputBuilder", FakeSimulationOutputBuilder)

    _run_simulation_impl_core(
        {"country": "us", "scope": "macro", "reform": {}},
        runtime=NoOpObservabilityRuntime(),
        progress=progress,
    )

    assert progress.calls == [
        ("enter", JobStage.BASELINE),
        ("enter", JobStage.REFORM),
    ]
    assert builders[0]["progress"] is progress


class _FakeCall:
    def __init__(self, result, polls_until_ready=0, error=None):
        self.result = result
        self.polls_until_ready = polls_until_ready
        self.error = error
        self.cancelled = False

    def get(self, timeout):  # noqa: ARG002  # mirrors modal.FunctionCall.get, which callers pass timeout= by keyword
        if self.error is not None:
            raise self.error
        if self.polls_until_ready > 0:
            self.polls_until_ready -= 1
            raise TimeoutError
        return self.result

    def cancel(self):
        self.cancelled = True


def _segmented_runner(calls, progress, monkeypatch):
    monkeypatch.setattr(
        sr,
        "_country_module",
        lambda _country: SimpleNamespace(
            model=SimpleNamespace(get_region=lambda _code: None, region_registry=None)
        ),
    )
    remaining = iter(calls)
    fake_modal = SimpleNamespace(
        Function=SimpleNamespace(
            from_name=lambda _app_name, _function_name: SimpleNamespace(
                spawn=lambda _payload, *, observability_context=None: next(remaining)  # noqa: ARG005  # mirrors modal.Function.spawn, which callers pass observability_context= by keyword
            )
        )
    )
    return sn.SegmentedNationalRunner(
        {"country": "us", "scope": "macro", "time_period": "2026"},
        app_name="policyengine-simulation-py-test",
        runtime=NoOpObservabilityRuntime(),
        modal_module=fake_modal,
        poll_interval_seconds=0.001,
        poll_interval_max_seconds=0.002,
        progress=progress,
    )


class TestSegmentedNationalStages:
    def test__reports_segments_then_outputs_before_the_reduce(self, monkeypatch):
        progress = _RecordingReporter()
        calls = [_FakeCall({"child": i}, polls_until_ready=i % 3) for i in range(20)]
        runner = _segmented_runner(calls, progress, monkeypatch)
        reduce_saw = []
        monkeypatch.setattr(
            runner,
            "_reduce",
            lambda _results, country_module: reduce_saw.append(list(progress.calls)),  # noqa: ARG005  # run() passes country_module= by keyword
        )

        runner.run()

        assert progress.calls[0] == ("start_segments", len(runner.groups))
        assert progress.calls.count(("segment_completed",)) == 20
        assert progress.calls[-1] == ("enter", JobStage.OUTPUTS)
        assert reduce_saw == [progress.calls]

    def test__child_failure_never_enters_outputs(self, monkeypatch):
        progress = _RecordingReporter()
        calls = [_FakeCall({"child": i}, polls_until_ready=5) for i in range(20)]
        calls[4] = _FakeCall(None, error=ValueError("boom"))
        runner = _segmented_runner(calls, progress, monkeypatch)

        with pytest.raises(RuntimeError):
            runner.run()

        assert ("enter", JobStage.OUTPUTS) not in progress.calls

    def test__dispatch_passes_the_reporter_to_the_monolithic_path(self, monkeypatch):
        progress = _RecordingReporter()
        seen = {}

        def fake_impl(_params, *, runtime, progress):  # noqa: ARG001  # keyword-only, as dispatch_run_simulation passes it
            seen["progress"] = progress
            return {"ok": True}

        monkeypatch.setattr(sr, "run_simulation_impl", fake_impl)

        sn.dispatch_run_simulation(
            {"country": "us", "scope": "macro", "region": "state/ut"},
            app_name="app-x",
            runtime=NoOpObservabilityRuntime(),
            progress=progress,
        )

        assert seen["progress"] is progress
        assert progress.calls == [("set_execution", JobExecution.MONOLITHIC)]

    def test__dispatch_marks_national_runs_as_segmented(self, monkeypatch):
        progress = _RecordingReporter()
        seen = {}

        def fake_segmented(_params, *, app_name, runtime, progress):  # noqa: ARG001  # keyword-only, as dispatch_run_simulation passes them
            seen["progress"] = progress
            return {"ok": True}

        monkeypatch.setattr(sn, "run_segmented_national_impl", fake_segmented)

        sn.dispatch_run_simulation(
            {"country": "us", "scope": "macro", "time_period": "2026"},
            app_name="app-x",
            runtime=NoOpObservabilityRuntime(),
            progress=progress,
        )

        assert seen["progress"] is progress
        assert progress.calls == [("set_execution", JobExecution.SEGMENTED)]


@pytest.fixture
def modal_app(monkeypatch):
    install_fake_modal(monkeypatch)
    for env_var, package in (
        ("POLICYENGINE_VERSION", "policyengine"),
        ("POLICYENGINE_CORE_VERSION", "policyengine-core"),
        ("POLICYENGINE_US_VERSION", "policyengine-us"),
        ("POLICYENGINE_UK_VERSION", "policyengine-uk"),
    ):
        monkeypatch.setenv(env_var, get_bundled_package_version(package))
    sys.modules.pop("src.modal.app", None)
    app = importlib.import_module("src.modal.app")
    yield app
    sys.modules.pop("src.modal.app", None)


class _OperationScope:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        self.events.append("operation_start")

    def __exit__(self, *exc_info):
        self.events.append("operation_end")
        return False


class _RecordingRuntime(NoOpObservabilityRuntime):
    def __init__(self, events):
        self.events = events

    def operation(self, *_args, **_kwargs):
        return _OperationScope(self.events)

    def set_context(self, **attributes):
        self.events.append(("context", attributes))

    def shutdown(self):
        self.events.append("shutdown")


def _stage_attributes(events) -> list[tuple[int, dict]]:
    return [
        (index, event[1])
        for index, event in enumerate(events)
        if isinstance(event, tuple)
        and any(key.startswith("job_stage_seconds_") for key in event[1])
    ]


class TestModalRunSimulation:
    def _wire(self, app, monkeypatch, dispatch, earlier=None):
        from policyengine_simulation_contract import job_progress_state

        events = []
        writes = []
        monkeypatch.setattr(
            app.modal, "current_function_call_id", lambda: "fc-job-1", raising=False
        )
        monkeypatch.setattr(
            job_progress_state,
            "put_job_progress",
            lambda job_id, record: writes.append((job_id, record)),
        )
        monkeypatch.setattr(
            job_progress_state, "get_job_progress", lambda _job_id: earlier
        )
        monkeypatch.setattr(
            app,
            "_configure_modal_observability",
            lambda *, service_role: _RecordingRuntime(events),  # noqa: ARG005  # keyword-only, as run_simulation passes it
        )
        monkeypatch.setattr(sn, "dispatch_run_simulation", dispatch)
        return events, writes

    def test__records_stages_for_the_spawned_job(self, modal_app, monkeypatch):
        def dispatch(_params, *, app_name, runtime, progress):  # noqa: ARG001  # keyword-only, as run_simulation passes them
            progress.enter(JobStage.REFORM)
            return {"ok": True}

        events, writes = self._wire(modal_app, monkeypatch, dispatch)

        assert modal_app.run_simulation({"country": "us"}) == {"ok": True}

        assert {job_id for job_id, _ in writes} == {"fc-job-1"}
        assert [record.stage for _, record in writes] == [
            JobStage.LOADING_DATA,
            JobStage.REFORM,
            JobStage.REFORM,
        ]
        assert writes[-1][1].finished_at is not None
        [(index, attributes)] = _stage_attributes(events)
        assert set(attributes) == {
            "job_attempts",
            "job_stage_seconds_loading_data",
            "job_stage_seconds_reform",
        }
        assert attributes["job_attempts"] == 1
        # Durations reach the operation before it ends, then the runtime stops.
        assert index < events.index("operation_end")
        assert events[-1] == "shutdown"

    def test__rerun_of_the_same_call_continues_its_record(self, modal_app, monkeypatch):
        def dispatch(_params, *, app_name, runtime, progress):  # noqa: ARG001  # keyword-only, as run_simulation passes them
            progress.set_execution(JobExecution.MONOLITHIC)
            return {"ok": True}

        events, writes = self._wire(
            modal_app, monkeypatch, dispatch, earlier=_earlier_attempt()
        )

        assert modal_app.run_simulation({"country": "us"}) == {"ok": True}

        assert writes[0][1].stage is JobStage.REFORM
        assert writes[0][1].attempts == 2
        [(_, attributes)] = _stage_attributes(events)
        assert attributes["job_attempts"] == 2

    def test__failed_job_still_records_where_it_stopped(self, modal_app, monkeypatch):
        def dispatch(_params, *, app_name, runtime, progress):  # noqa: ARG001  # keyword-only, as run_simulation passes them
            progress.enter(JobStage.BASELINE)
            msg = "model failed"
            raise RuntimeError(msg)

        events, writes = self._wire(modal_app, monkeypatch, dispatch)

        with pytest.raises(RuntimeError, match="model failed"):
            modal_app.run_simulation({"country": "us"})

        assert writes[-1][1].stage is JobStage.BASELINE
        assert writes[-1][1].finished_at is not None
        [(index, attributes)] = _stage_attributes(events)
        assert "job_stage_seconds_baseline" in attributes
        assert index < events.index("operation_end")

    def test__preempted_job_stays_running_for_the_next_attempt(
        self, modal_app, monkeypatch
    ):
        def dispatch(_params, *, app_name, runtime, progress):  # noqa: ARG001  # keyword-only, as run_simulation passes them
            progress.enter(JobStage.BASELINE)
            raise KeyboardInterrupt

        events, writes = self._wire(modal_app, monkeypatch, dispatch)

        with pytest.raises(KeyboardInterrupt):
            modal_app.run_simulation({"country": "us"})

        assert writes[-1][1].stage is JobStage.BASELINE
        assert writes[-1][1].finished_at is None
        # The preempted attempt's durations still reach its operation.
        [(index, attributes)] = _stage_attributes(events)
        assert "job_stage_seconds_baseline" in attributes
        assert index < events.index("operation_end")
        assert events[-1] == "shutdown"

    def test__preemption_before_the_calculation_stays_running(
        self, modal_app, monkeypatch
    ):
        def dispatch(_params, *, app_name, runtime, progress):  # noqa: ARG001  # keyword-only, as run_simulation passes them
            msg = "dispatch should not run"
            raise AssertionError(msg)

        _, writes = self._wire(
            modal_app, monkeypatch, dispatch, earlier=_earlier_attempt()
        )

        def preempted(_runtime):
            raise KeyboardInterrupt

        monkeypatch.setattr(modal_app, "_set_modal_call_attributes", preempted)

        with pytest.raises(KeyboardInterrupt):
            modal_app.run_simulation({"country": "us"})

        assert writes[-1][1].finished_at is None
        assert writes[-1][1].stage is JobStage.REFORM
        assert writes[-1][1].attempts == 2

    def test__store_failure_does_not_change_the_result(self, modal_app, monkeypatch):
        from policyengine_simulation_contract import job_progress_state

        def dispatch(_params, *, app_name, runtime, progress):  # noqa: ARG001  # keyword-only, as run_simulation passes them
            progress.enter(JobStage.OUTPUTS)
            return {"ok": True}

        self._wire(modal_app, monkeypatch, dispatch)

        def failing_write(_job_id, _record):
            msg = "modal unavailable"
            raise ConnectionError(msg)

        monkeypatch.setattr(job_progress_state, "put_job_progress", failing_write)

        assert modal_app.run_simulation({"country": "us"}) == {"ok": True}

    def test__no_call_id_means_no_reporting(self, modal_app, monkeypatch):
        def missing_call_id():
            msg = "not inside a Modal function call"
            raise RuntimeError(msg)

        monkeypatch.setattr(
            modal_app.modal, "current_function_call_id", missing_call_id, raising=False
        )

        assert modal_app._job_progress_reporter() is NULL_JOB_PROGRESS
