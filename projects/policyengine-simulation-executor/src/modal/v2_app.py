# pyright: reportAttributeAccessIssue=false
"""Separately named v2 Modal application: report coordinator and country workers.

A simulation worker's call calculates a simulation run, or, when its context
names a cliff result, that run's cliff gap and share. The simulation workers
are methods of Modal classes (``SimulationWorkerUS``, ``SimulationWorkerUK``
and ``SimulationSegmentWorkerUS``), whose containers restore from a memory
snapshot with their country's models loaded; their callers find each class
in ``policyengine_simulation_contract.stage12_simulation_workers``. Besides
the Stage 12 report coordinator and simulation workers, it defines a
household function for each country, ``calculate_household_us`` and
``calculate_household_uk``, in an image without datasets. A call calculates
one household under one policy, and records it on the household job it
names; or it calculates a household report run's baseline and, if it has
one, its reform, and records them on the report run and its simulation
runs. Both record with the Modal worker role's database secret, the
functions' only secret. A call that names neither, as deployment
validation makes, records nothing.

This module defines additional resources. It does not rename or modify the
existing ``policyengine-simulation-py{version}`` applications.
"""

from __future__ import annotations

import contextlib
import os
import random
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from policyengine_simulation_contract.household_execution import (
    household_callable_name,
)
from policyengine_simulation_contract.stage12_manifest import v2_application_name
from policyengine_simulation_executor import stage12_runtime
from policyengine_simulation_executor.stage12_runtime import report_run_access
from policyengine_simulation_executor.stage12_runtime.report_run_access import (
    MODAL_WORKER_DATABASE_SECRET_NAME,
    MODAL_WORKER_DATABASE_URL_ENV,
)
from policyengine_simulation_executor.stage12_timeouts import (
    COORDINATOR_TIMEOUT_SECONDS,
    HOUSEHOLD_FUNCTION_TIMEOUT_SECONDS,
    UK_SIMULATION_TIMEOUT_SECONDS,
    US_SIMULATION_TIMEOUT_SECONDS,
)
from policyengine_simulation_observability.observability import (
    init_process_observability,
)
from policyengine_simulation_observability.stages import (
    STAGE12_CANONICAL_REPORT_STAGES,
    STAGE12_SEGMENT_STAGES,
    STAGE12_SHADOW_REPORT_STAGES,
    STAGE12_SIMULATION_STAGES,
    V2_HOUSEHOLD_STAGES,
    Stage,
)
from policyengine_simulation_observability.telemetry import (
    normalize_observability_context,
)
from src.modal.container_calls import ContainerCalls
from src.modal.country_model_preload import preload_country_models
from src.modal.v2_images import (
    BUNDLE_VALUES,
    RESOLVED_BUNDLE,
    STAGE12_CACHE_BUCKET,
    STAGE12_CACHE_CONTENTS,
    STAGE12_CACHE_MANIFEST,
    STAGE12_DATA_DIR,
    build_household_image,
    build_v2_image,
    coordinator_image,
    gcp_secret,
    hf_secret,
    household_image,
    uk_worker_image,
    us_worker_image,
)

import modal

if TYPE_CHECKING:
    from collections.abc import Callable

    from policyengine_simulation_contract.household_execution import (
        HouseholdExecutionFailure,
        HouseholdExecutionResult,
    )
    from policyengine_simulation_contract.stage12_bundle import CountryId
    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
    )
    from policyengine_simulation_executor.stage12_runtime.simulation import (
        SimulationCalculation,
    )

# The images and the deploy-time values their build steps read live in
# src/modal/v2_images.py; this module keeps their names for the smoke apps and
# other code that imports them from here.
__all__ = [
    "BUNDLE_VALUES",
    "RESOLVED_BUNDLE",
    "STAGE12_CACHE_BUCKET",
    "STAGE12_CACHE_CONTENTS",
    "STAGE12_CACHE_MANIFEST",
    "STAGE12_DATA_DIR",
    "build_household_image",
    "build_v2_image",
    "coordinator_image",
    "gcp_secret",
    "hf_secret",
    "household_image",
    "uk_worker_image",
    "us_worker_image",
]

APP_NAME = v2_application_name(RESOLVED_BUNDLE.bundle.policyengine_version)


if os.environ.get("MODAL_APP_NAME") not in {None, "", APP_NAME}:
    msg = "MODAL_APP_NAME does not match the bundle-derived v2 name"
    raise RuntimeError(msg)


app = modal.App(APP_NAME)
comparison_runtime_secret = modal.Secret.from_name("stage12-evaluation-runtime")
worker_secrets = [
    gcp_secret,
    hf_secret,
    comparison_runtime_secret,
]
# Only the functions that record a report run's, simulation run's, cliff
# result's or household job's status and results hold the Modal worker role's
# database credentials.
modal_worker_database_secret = modal.Secret.from_name(
    MODAL_WORKER_DATABASE_SECRET_NAME,
    required_keys=[MODAL_WORKER_DATABASE_URL_ENV],
)
report_run_writer_secrets = [*worker_secrets, modal_worker_database_secret]
# How this container started, and its calls' numbers: one worker per container.
_CONTAINER_CALLS = ContainerCalls()


def _validate(country: CountryId) -> dict:
    from policyengine_simulation_executor.stage12_worker_validation import (  # noqa: PLC0415  # Modal function body: loaded in the worker container, not at deploy time
        validate_country_worker,
    )

    return validate_country_worker(
        country=country,
        expected_bundle_manifest_sha256=os.environ["STAGE12_BUNDLE_MANIFEST_SHA256"],
        expected_cache_manifest_sha256=(
            os.environ.get("STAGE12_CACHE_MANIFEST_DIGEST") if country == "us" else None
        ),
    )


@app.function(
    image=us_worker_image,
    cpu=2.0,
    memory=4096,
    timeout=900,
    retries=0,
    secrets=worker_secrets,
)
def validate_worker_us() -> dict:
    """Validate the US worker image without serving and return its bundle values."""
    return _validate("us")


@app.function(
    image=uk_worker_image,
    cpu=2.0,
    memory=4096,
    timeout=900,
    retries=0,
    secrets=worker_secrets,
)
def validate_worker_uk() -> dict:
    """Validate the UK worker image without serving and return its bundle values."""
    return _validate("uk")


# Each simulation worker is a method of a Modal class whose containers restore
# from a memory snapshot with the country's models loaded, and a plain function
# of the same name and options, without a snapshot, for callers still running
# code from before the classes: an entry service revision (the stable one
# during a deploy, or one a rollback routes traffic back to), and a coordinator
# or US worker call running while this app is redeployed in place. Both run the
# same calculation.
_SEGMENT_WORKER_OPTIONS: dict[str, Any] = {
    "image": us_worker_image,
    "cpu": 8.0,
    "memory": 32768,
    "timeout": 3600,
    "retries": 0,
    # Every segment of every US simulation call that may run at once: a
    # segment queued behind this limit counts against its caller's wait.
    "max_containers": 1000,
    "secrets": worker_secrets,
}
_US_SIMULATION_WORKER_OPTIONS: dict[str, Any] = {
    "image": us_worker_image,
    "cpu": 8.0,
    "memory": 32768,
    "timeout": US_SIMULATION_TIMEOUT_SECONDS,
    "retries": 0,
    # Both simulations of every coordinator call that may run at once. The
    # simulation runs and cliff calculations the simulation service starts
    # directly share the same containers.
    "max_containers": 50,
    "secrets": report_run_writer_secrets,
}
_UK_SIMULATION_WORKER_OPTIONS: dict[str, Any] = {
    **_US_SIMULATION_WORKER_OPTIONS,
    "image": uk_worker_image,
    "timeout": UK_SIMULATION_TIMEOUT_SECONDS,
}


def _run_single_simulation_segment_us(
    payload: dict,
    *,
    observability_context: dict | None = None,
    simulation_run_id: str | None = None,
) -> dict:
    """Calculate one US region-group segment and return its artifact descriptor.

    ``simulation_run_id`` names the detailed-save simulation run the segment
    calculates part of, for its telemetry only.
    """
    runtime = init_process_observability(
        service_name="policyengine-stage12-us-segment-worker",
        service_role="stage12_segment_worker",
        platform="modal",
        environment=os.getenv("MODAL_ENVIRONMENT", "local"),
    )

    try:
        propagated = normalize_observability_context(observability_context)
        with runtime.operation(
            STAGE12_SEGMENT_STAGES.name(Stage.STAGE12_SEGMENT_EXECUTION),
            attributes={
                "runner_name": "stage12",
                "simulation_role": payload.get("simulation", {}).get("role"),
                "segment_index": payload.get("segment_index"),
                **stage12_runtime.operation_record_attributes(
                    "simulation_runs",
                    stage12_runtime.row_id_or_none(simulation_run_id),
                    modal.current_function_call_id(),
                ),
            },
            remote_context=propagated,
        ):
            _CONTAINER_CALLS.set_call_attributes(runtime)
            return stage12_runtime.calculate_segment(
                payload, runtime=runtime
            ).model_dump(mode="python")
    finally:
        runtime.shutdown()


def _run_single_simulation_us(
    payload: dict,
    context: dict,
    *,
    observability_context: dict | None = None,
) -> dict:
    """Calculate one US simulation and return its artifact descriptor.

    A ``context`` naming a simulation run claims that run under this call's
    Modal call ID; a simulation started on its own, without an output plan,
    is planned here. A ``context`` naming a cliff result instead calculates
    the cliff gap and share of that result's simulation run, ``payload``,
    and records them on the result (``stage12_runtime.cliff_results``).
    """
    runtime = init_process_observability(
        service_name="policyengine-stage12-us-worker",
        service_role="stage12_simulation_worker",
        platform="modal",
        environment=os.getenv("MODAL_ENVIRONMENT", "local"),
    )

    # Read inside ``try``, so the runtime is shut down whatever happens.
    simulation_run_id = None

    def segmented_calculator(
        simulation: PlannedSimulationExecutionInput,
        cancellation_requested: Callable[[], bool],
    ) -> SimulationCalculation:
        return stage12_runtime.run_segmented_simulation(
            simulation,
            app_name=APP_NAME,
            runtime=runtime,
            cancellation_requested=cancellation_requested,
            simulation_run_id=simulation_run_id,
        )

    try:
        modal_call_id = modal.current_function_call_id()
        simulation_run_id = stage12_runtime.simulation_run_named_by(context)
        propagated = normalize_observability_context(observability_context)
        with runtime.operation(
            STAGE12_SIMULATION_STAGES.name(Stage.STAGE12_SIMULATION_EXECUTION),
            attributes={
                "runner_name": "stage12",
                "simulation_role": payload.get("role"),
                **stage12_runtime.simulation_worker_record_attributes(
                    context, modal_call_id
                ),
            },
            remote_context=propagated,
        ):
            _CONTAINER_CALLS.set_call_attributes(runtime)
            return stage12_runtime.run_single_simulation(
                payload,
                context,
                required_country="us",
                dependencies=stage12_runtime.SimulationWorkerDependencies(
                    segmented_calculator=segmented_calculator,
                    runtime=runtime,
                ),
                modal_call_id=modal_call_id,
            )
    finally:
        runtime.shutdown()


def _run_single_simulation_uk(
    payload: dict,
    context: dict,
    *,
    observability_context: dict | None = None,
) -> dict:
    """Calculate one UK simulation and return its artifact descriptor.

    A ``context`` naming a simulation run claims that run under this call's
    Modal call ID; a simulation started on its own, without an output plan,
    is planned here. A ``context`` naming a cliff result instead calculates
    the cliff gap and share of that result's simulation run, ``payload``,
    and records them on the result (``stage12_runtime.cliff_results``).
    """
    runtime = init_process_observability(
        service_name="policyengine-stage12-uk-worker",
        service_role="stage12_simulation_worker",
        platform="modal",
        environment=os.getenv("MODAL_ENVIRONMENT", "local"),
    )
    try:
        modal_call_id = modal.current_function_call_id()
        propagated = normalize_observability_context(observability_context)
        with runtime.operation(
            STAGE12_SIMULATION_STAGES.name(Stage.STAGE12_SIMULATION_EXECUTION),
            attributes={
                "runner_name": "stage12",
                "simulation_role": payload.get("role"),
                **stage12_runtime.simulation_worker_record_attributes(
                    context, modal_call_id
                ),
            },
            remote_context=propagated,
        ):
            _CONTAINER_CALLS.set_call_attributes(runtime)
            return stage12_runtime.run_single_simulation(
                payload,
                context,
                required_country="uk",
                dependencies=stage12_runtime.SimulationWorkerDependencies(
                    runtime=runtime
                ),
                modal_call_id=modal_call_id,
            )
    finally:
        runtime.shutdown()


class _V2CountryModelsLoadedBeforeSnapshot:
    """Base of the v2 simulation workers: loads the models before the snapshot.

    Modal runs ``snap=True`` hooks in every container that isn't restored, and
    takes the memory snapshot from such a container when it starts it with
    snapshots enabled; every container restored from the snapshot starts with
    the loaded models in memory. ``snap=False`` hooks run after each restore.
    Observability is configured per call instead, after the restore, so each
    restored container reports its own process identity.
    """

    # The image's own country: a failed import of one country's models can't
    # affect the other country's workers. Annotated with names Python can
    # resolve when it runs, since Modal reads class annotations.
    snapshot_countries: ClassVar[tuple[str, ...]] = ()

    @modal.enter(snap=True)
    def load_country_models(self) -> None:
        """Load the class's country models, and for the US the SPM capability."""
        # For each call's container_start; a failed record never stops the preload.
        with contextlib.suppress(Exception):
            _CONTAINER_CALLS.record_snapshot_hook()
        preload_country_models(self.snapshot_countries)

    @modal.enter(snap=False)
    def reseed_random_after_restore(self) -> None:
        """Give each restored container its own random sequence.

        The snapshot holds the state of Python's ``random`` module, and Python
        reseeds it only after a fork, so every restored container would draw
        the same numbers: OpenTelemetry's span IDs, for one, and the delays of
        the database and dataset download retries.
        """
        random.seed()


@app.cls(**_SEGMENT_WORKER_OPTIONS, enable_memory_snapshot=True)
class SimulationSegmentWorkerUS(_V2CountryModelsLoadedBeforeSnapshot):
    """Calculates the region-group segments of segmented US simulations.

    A separate class from ``SimulationWorkerUS``, so the segments draw from
    their own container pool: a US simulation waiting on its segments must
    never share a bounded pool with them.
    """

    snapshot_countries = ("us",)

    @modal.method()
    def run_single_simulation_segment_us(
        self,
        payload: dict,
        *,
        observability_context: dict | None = None,
        simulation_run_id: str | None = None,
    ) -> dict:
        """Calculate one segment (see ``_run_single_simulation_segment_us``)."""
        return _run_single_simulation_segment_us(
            payload,
            observability_context=observability_context,
            simulation_run_id=simulation_run_id,
        )


@app.cls(**_US_SIMULATION_WORKER_OPTIONS, enable_memory_snapshot=True)
class SimulationWorkerUS(_V2CountryModelsLoadedBeforeSnapshot):
    """Calculates US simulations, cliff calculations included."""

    snapshot_countries = ("us",)

    @modal.method()
    def run_single_simulation_us(
        self,
        payload: dict,
        context: dict,
        *,
        observability_context: dict | None = None,
    ) -> dict:
        """Calculate one US simulation (see ``_run_single_simulation_us``)."""
        return _run_single_simulation_us(
            payload, context, observability_context=observability_context
        )


@app.cls(**_UK_SIMULATION_WORKER_OPTIONS, enable_memory_snapshot=True)
class SimulationWorkerUK(_V2CountryModelsLoadedBeforeSnapshot):
    """Calculates UK simulations, cliff calculations included."""

    snapshot_countries = ("uk",)

    @modal.method()
    def run_single_simulation_uk(
        self,
        payload: dict,
        context: dict,
        *,
        observability_context: dict | None = None,
    ) -> dict:
        """Calculate one UK simulation (see ``_run_single_simulation_uk``)."""
        return _run_single_simulation_uk(
            payload, context, observability_context=observability_context
        )


@app.function(**_SEGMENT_WORKER_OPTIONS)
def run_single_simulation_segment_us(
    payload: dict,
    *,
    observability_context: dict | None = None,
    simulation_run_id: str | None = None,
) -> dict:
    """``SimulationSegmentWorkerUS``'s method, for callers on older code."""
    return _run_single_simulation_segment_us(
        payload,
        observability_context=observability_context,
        simulation_run_id=simulation_run_id,
    )


@app.function(**_US_SIMULATION_WORKER_OPTIONS)
def run_single_simulation_us(
    payload: dict,
    context: dict,
    *,
    observability_context: dict | None = None,
) -> dict:
    """``SimulationWorkerUS``'s method, for callers on older code."""
    return _run_single_simulation_us(
        payload, context, observability_context=observability_context
    )


@app.function(**_UK_SIMULATION_WORKER_OPTIONS)
def run_single_simulation_uk(
    payload: dict,
    context: dict,
    *,
    observability_context: dict | None = None,
) -> dict:
    """``SimulationWorkerUK``'s method, for callers on older code."""
    return _run_single_simulation_uk(
        payload, context, observability_context=observability_context
    )


@app.function(
    image=coordinator_image,
    cpu=2.0,
    memory=8192,
    # The shared child-wait window (the longest simulation worker timeout),
    # the production-result wait, and time to finish and record the report.
    timeout=COORDINATOR_TIMEOUT_SECONDS,
    retries=0,
    # Three budget windows at five years each, and ten other reports.
    max_containers=25,
    secrets=report_run_writer_secrets,
)
def coordinate_report(
    payload: dict,
    context: dict,
    parent: dict | None = None,
    *,
    observability_context: dict | None = None,
) -> dict:
    """Run one Stage 12 report: reuse or fan out its simulations, aggregate, record.

    A detailed-save call's ``context`` names its report run, and it passes no
    ``parent``: the coordinator claims the report run under its Modal call
    ID, uses the stored output of each simulation run that already
    succeeded, and starts a worker for each other one, which claims its own
    simulation run. A comparison-path call passes its ``parent`` evaluation
    record; a ``production_function_call_id`` in its ``context`` marks a
    shadow run whose result is compared with that production job's. A report
    run or evaluation record another call holds, or one already finished,
    returns a ``deduplicated`` marker instead.
    """
    coordinator_invocation_id = modal.current_function_call_id()
    if coordinator_invocation_id is None:
        msg = "Modal coordinator invocation identifier is unavailable"
        raise RuntimeError(msg)
    runtime = init_process_observability(
        service_name="policyengine-stage12-coordinator",
        service_role="stage12_report_coordinator",
        platform="modal",
        environment=os.getenv("MODAL_ENVIRONMENT", "local"),
    )
    stage_plan = (
        STAGE12_SHADOW_REPORT_STAGES
        if context.get("production_function_call_id")
        else STAGE12_CANONICAL_REPORT_STAGES
    )
    try:
        propagated = normalize_observability_context(observability_context)
        with runtime.operation(
            stage_plan.name(Stage.STAGE12_COORDINATOR_EXECUTION),
            attributes={
                "runner_name": "stage12",
                "execution_mode": (
                    "shadow"
                    if context.get("production_function_call_id")
                    else "authoritative"
                ),
                **stage12_runtime.operation_record_attributes(
                    "report_runs",
                    stage12_runtime.report_run_named_by(context),
                    coordinator_invocation_id,
                ),
            },
            remote_context=propagated,
        ):
            # A call naming a budget window's year marks the window running
            # first and, when the year ends, starts the window's next years.
            return stage12_runtime.coordinate_window_year(
                lambda: stage12_runtime.coordinate_report(
                    payload,
                    context,
                    parent,
                    invocation=stage12_runtime.CoordinatorInvocation(
                        application_name=APP_NAME,
                        invocation_id=coordinator_invocation_id,
                    ),
                    dependencies=stage12_runtime.CoordinatorDependencies(
                        runtime=runtime
                    ),
                ),
                payload=payload,
                context=context,
                windows=lambda: report_run_access.budget_window_store(runtime),
                observability_context=runtime.capture_context,
            )
    finally:
        runtime.shutdown()


# Household function sizing. A call holds one country model and a simulation
# of the household, under one policy, at every axis point; a household report
# run's call calculates its baseline and then its reform, which the
# simulation service builds without axes. Each container
# reserves 4 GiB and may use up to 16 GiB before Modal stops it, so a large
# household calculated with every variable at many axis points still fits.
# The timeout (stage12_timeouts.HOUSEHOLD_FUNCTION_TIMEOUT_SECONDS) is three
# times the Public API's 300-second request timeout; a household report run's
# two calculations, as the simulation service builds them without axes, take a
# small part of it. A report run's call may also wait, until two minutes before
# it, for a side another call holds (HOUSEHOLD_SIDE_WAIT_SECONDS), holding one
# of these containers meanwhile.
HOUSEHOLD_FUNCTION_CPU = 2.0
HOUSEHOLD_FUNCTION_MEMORY_MIB = (4096, 16384)
HOUSEHOLD_FUNCTION_MAX_CONTAINERS = 20


def _run_household_calculation(
    country: CountryId,
    payload: dict,
    *,
    observability_context: dict | None,
    household_job_id: str | None,
    household_report_run_id: str | None = None,
) -> dict:
    """Calculate one household request for ``country`` and return its outcome.

    The outcome is a ``HouseholdExecutionResult`` or ``HouseholdExecutionFailure``
    as JSON. Raises for a malformed request or a request for another country.
    With ``household_job_id``, the call claims that household job under its
    Modal call ID and records the outcome on it, or returns the job's status
    without calculating when another call holds it or it has finished
    (``stage12_runtime.run_household_job``). With ``household_report_run_id``,
    ``payload`` is a ``HouseholdReportRunCalculation``: the call claims the
    report run and each side's simulation run it can, calculates and records
    those sides, waits for a side another call holds, and returns the runs'
    statuses
    (``stage12_runtime.run_household_report``). A call names at most one of
    the two; without either, the call touches no database.
    """
    from policyengine_simulation_contract.household_execution import (  # noqa: PLC0415  # Modal function body: loaded in the worker container, not at deploy time
        HouseholdExecutionRequest,
        HouseholdWorkerIdentity,
    )
    from policyengine_simulation_executor import (  # noqa: PLC0415  # Modal function body: loaded in the worker container, not at deploy time
        household_calculation,
    )

    runtime = init_process_observability(
        service_name=f"policyengine-v2-household-{country}-worker",
        service_role="v2_household_worker",
        platform="modal",
        environment=os.getenv("MODAL_ENVIRONMENT", "local"),
    )

    def calculate_request(
        request: HouseholdExecutionRequest,
    ) -> HouseholdExecutionResult | HouseholdExecutionFailure:
        return household_calculation.calculate_household(
            request,
            worker=HouseholdWorkerIdentity(
                application_name=APP_NAME,
                function_name=household_callable_name(country),
            ),
            required_country=country,
            dependencies=household_calculation.HouseholdCalculationDependencies(
                runtime=runtime
            ),
        )

    def calculate() -> HouseholdExecutionResult | HouseholdExecutionFailure:
        return calculate_request(HouseholdExecutionRequest.model_validate(payload))

    try:
        if household_job_id is not None and household_report_run_id is not None:
            msg = "a household call names either a household job or a report run"
            raise ValueError(msg)
        propagated = normalize_observability_context(observability_context)
        modal_call_id = modal.current_function_call_id()
        # A household report run is a ``report_runs`` row.
        record_attributes = (
            stage12_runtime.operation_record_attributes(
                "report_runs",
                stage12_runtime.row_id_or_none(household_report_run_id),
                modal_call_id,
            )
            if household_report_run_id is not None
            else stage12_runtime.operation_record_attributes(
                "household_jobs",
                stage12_runtime.row_id_or_none(household_job_id),
                modal_call_id,
            )
        )
        result = None
        with runtime.operation(
            V2_HOUSEHOLD_STAGES.name(Stage.V2_HOUSEHOLD_EXECUTION),
            attributes={"country": country, **record_attributes},
            remote_context=propagated,
        ):
            if household_report_run_id is not None:
                # The recording code validates the payload once it holds
                # the report run, so a refused payload fails the run.
                result = stage12_runtime.run_household_report(
                    UUID(household_report_run_id),
                    payload,
                    calculate_request,
                    modal_call_id=modal_call_id,
                    runtime=runtime,
                )
            elif household_job_id is None:
                result = calculate().model_dump(mode="json")
            else:
                result = stage12_runtime.run_household_job(
                    UUID(household_job_id),
                    calculate,
                    modal_call_id=modal_call_id,
                    runtime=runtime,
                )
        if result is None:
            msg = f"the {country} household calculation returned no result"
            raise RuntimeError(msg)
        return result
    finally:
        runtime.shutdown()


@app.function(
    image=household_image,
    cpu=HOUSEHOLD_FUNCTION_CPU,
    memory=HOUSEHOLD_FUNCTION_MEMORY_MIB,
    timeout=HOUSEHOLD_FUNCTION_TIMEOUT_SECONDS,
    retries=0,
    max_containers=HOUSEHOLD_FUNCTION_MAX_CONTAINERS,
    secrets=[modal_worker_database_secret],
)
def calculate_household_us(
    payload: dict,
    *,
    observability_context: dict | None = None,
    household_job_id: str | None = None,
    household_report_run_id: str | None = None,
) -> dict:
    """Calculate one US household under the request's policy.

    With ``household_job_id``, record the calculation on that household job.
    With ``household_report_run_id``, ``payload`` is that household report
    run's calculation: calculate and record each side, then the report run.
    """
    return _run_household_calculation(
        "us",
        payload,
        observability_context=observability_context,
        household_job_id=household_job_id,
        household_report_run_id=household_report_run_id,
    )


@app.function(
    image=household_image,
    cpu=HOUSEHOLD_FUNCTION_CPU,
    memory=HOUSEHOLD_FUNCTION_MEMORY_MIB,
    timeout=HOUSEHOLD_FUNCTION_TIMEOUT_SECONDS,
    retries=0,
    max_containers=HOUSEHOLD_FUNCTION_MAX_CONTAINERS,
    secrets=[modal_worker_database_secret],
)
def calculate_household_uk(
    payload: dict,
    *,
    observability_context: dict | None = None,
    household_job_id: str | None = None,
    household_report_run_id: str | None = None,
) -> dict:
    """Calculate one UK household under the request's policy.

    With ``household_job_id``, record the calculation on that household job.
    With ``household_report_run_id``, ``payload`` is that household report
    run's calculation: calculate and record each side, then the report run.
    """
    return _run_household_calculation(
        "uk",
        payload,
        observability_context=observability_context,
        household_job_id=household_job_id,
        household_report_run_id=household_report_run_id,
    )
