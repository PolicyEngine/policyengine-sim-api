"""The scheduled restart check: its Modal wiring and its telemetry.

The stuck-run sweep's Modal application runs ``run_restart_check`` every two
minutes, as the Modal worker role, from the sweep's Modal secret. It builds
the restart service on the run tables (``scheduled_restart_service``) and
runs one check (``run_scheduled_check``) in its own telemetry operation.

How often runs need a restart is the check's telemetry:

- each check is one ``v2_run_restart_check`` operation, with the number of
  rows it listed and how each ended (``restart_listed``, ``restart_failed``
  and the rest) set on it;
- each row or budget window it takes up is one ``v2_run_restart``
  operation, with its kind, ID and age (for a window, the time since its
  last change). A row left ``deferred`` is taken up again by the next
  check, so the operation count metric counts restart attempts, and the
  distinct ``run_id`` values are the runs whose call never started that a
  check took up. A row no check took up (more rows at once than the checks
  in its window had time for) is failed by the sweep with
  ``run_not_started`` instead; a count of the outcomes other than
  ``deferred`` also leaves out the rows deferred in every check.
- each outcome is set on its operation (``restart_outcome``, with
  ``restart_error_type`` when an error ended it) and logged as a
  ``v2_run_restart_outcome`` event, at WARNING when the row or window could
  not be started again or its rows could not be read; an unexpected error
  in a restart is also logged with its traceback
  (``v2_run_restart_unexpected_error``).

This module imports none of the web service, so the sweep's image, which
has none of its packages, can run it.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from policyengine_simulation_contract.stage12_manifest import V2_VERSION_MANIFEST_NAME
from policyengine_simulation_observability.stages import (
    V2_RUN_RESTART_STAGES,
    Stage,
)
from policyengine_simulation_persistence import (
    EconomyRunStore,
    HouseholdJobStore,
    RunRestartStore,
    create_simulation_engine,
)
from policyengine_stage12_persistence import BudgetWindowStore

from policyengine_simulation_entry.economy_workers import (
    ManifestEconomyWorkers,
    ModalEconomyRunCaller,
)
from policyengine_simulation_entry.household_workers import (
    ManifestHouseholdWorkers,
    ModalHouseholdFunctionCaller,
)
from policyengine_simulation_entry.run_restarts import (
    RestartCallers,
    RestartOutcome,
    RestartSettings,
    RestartStores,
    RestartWorkers,
    RunRestartService,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from contextlib import AbstractContextManager

    from sqlalchemy import Engine

    from policyengine_simulation_entry.run_restarts import (
        RestartSummary,
        RestartTarget,
    )

# The longest any one statement of a check may take.
RESTART_STATEMENT_TIMEOUT_MILLISECONDS: Final = 5_000
OUTCOME_EVENT: Final = "v2_run_restart_outcome"
# The outcomes worth a warning: the row could not be started again, or the
# check could not tell, so a person may need to look.
WARNING_OUTCOMES: Final = frozenset(
    {
        RestartOutcome.FAILED,
        RestartOutcome.DEFERRED,
        RestartOutcome.CALL_NOT_RECORDED,
    }
)


class RestartRuntime(Protocol):
    """What the check's telemetry uses of ``ObservabilityRuntime``."""

    def operation(
        self, name: str, *, attributes: Mapping[str, Any] | None = None
    ) -> AbstractContextManager[Any]:
        """Return the scope of one operation."""
        ...

    def set_context(self, **attributes: Any) -> None:  # noqa: ANN401  # the runtime's own signature
        """Set attributes on the innermost open operation."""
        ...

    def event(
        self,
        name: str,
        *,
        severity: str = "INFO",
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        """Log one structured event."""
        ...

    def capture_context(self) -> dict[str, str]:
        """Return the context a call started now continues from."""
        ...


class ObservabilityRestartTelemetry:
    """Report each listed row as an operation, and its outcome as an event."""

    def __init__(self, runtime: RestartRuntime) -> None:
        """Report through ``runtime``, which never raises."""
        self._runtime = runtime

    def restart_scope(
        self, run: RestartTarget, *, age_seconds: float
    ) -> AbstractContextManager[Any]:
        """Return the operation one row's or window's restart runs in."""
        return self._runtime.operation(
            V2_RUN_RESTART_STAGES.name(Stage.V2_RUN_RESTART),
            attributes={
                "run_kind": run.kind.value,
                "run_id": str(run.run_id),
                "run_age_seconds": age_seconds,
            },
        )

    def record_outcome(
        self,
        run: RestartTarget,
        outcome: RestartOutcome,
        *,
        error_type: str | None = None,
    ) -> None:
        """Set the outcome on the row's operation and log it as an event."""
        outcome_attributes: dict[str, str] = {"restart_outcome": outcome.value}
        if error_type is not None:
            outcome_attributes["restart_error_type"] = error_type
        self._runtime.set_context(**outcome_attributes)
        self._runtime.event(
            OUTCOME_EVENT,
            severity="WARNING" if outcome in WARNING_OUTCOMES else "INFO",
            attributes={
                "run_kind": run.kind.value,
                "run_id": str(run.run_id),
                **outcome_attributes,
            },
        )

    def capture_context(self) -> dict[str, str] | None:
        """Return the context a restarted call continues the check's trace from."""
        return self._runtime.capture_context()


class RestartCheck(Protocol):
    """One restart check (``RunRestartService``)."""

    async def restart_unstarted_runs(self) -> RestartSummary:
        """Restart the rows whose call never started; count how each ended."""
        ...


def run_scheduled_check(
    service: RestartCheck, runtime: RestartRuntime
) -> dict[str, int]:
    """Run one check in its own operation; return how many rows ended each way."""
    with runtime.operation(V2_RUN_RESTART_STAGES.name(Stage.V2_RUN_RESTART_CHECK)):
        summary = asyncio.run(service.restart_unstarted_runs())
        counts = asdict(summary)
        runtime.set_context(
            **{f"restart_{name}": count for name, count in counts.items()}
        )
    return counts


@dataclass(frozen=True)
class ScheduledRestart:
    """The restart service on the run tables, and the engine it connects through."""

    service: RunRestartService
    settings: RestartSettings
    engine: Engine


def scheduled_restart_service(
    *, database_url: str, modal_environment: str, runtime: RestartRuntime
) -> ScheduledRestart:
    """Build the restart service for the Modal environment's deployment.

    ``database_url`` is the Modal worker role's ``postgresql://`` URL. Nothing
    connects to the database or reads Modal until the check runs. Raise
    ValueError for a Modal environment no deployment uses, or a malformed URL.
    """
    settings = RestartSettings.for_modal_environment(modal_environment)
    engine = create_simulation_engine(database_url)
    timeout = RESTART_STATEMENT_TIMEOUT_MILLISECONDS
    service = RunRestartService(
        stores=RestartStores(
            restarts=RunRestartStore.from_engine(
                engine, statement_timeout_milliseconds=timeout
            ),
            economy=EconomyRunStore.from_engine(
                engine, statement_timeout_milliseconds=timeout
            ),
            households=HouseholdJobStore.from_engine(
                engine, statement_timeout_milliseconds=timeout
            ),
            windows=BudgetWindowStore(
                engine=engine, statement_timeout_milliseconds=timeout
            ),
        ),
        workers=RestartWorkers(
            economy=ManifestEconomyWorkers.from_modal(
                manifest_name=V2_VERSION_MANIFEST_NAME, environment=modal_environment
            ),
            households=ManifestHouseholdWorkers.from_modal(
                manifest_name=V2_VERSION_MANIFEST_NAME, environment=modal_environment
            ),
        ),
        callers=RestartCallers(
            economy=ModalEconomyRunCaller(modal_environment),
            households=ModalHouseholdFunctionCaller(modal_environment),
        ),
        settings=settings,
        telemetry=ObservabilityRestartTelemetry(runtime),
    )
    return ScheduledRestart(service=service, settings=settings, engine=engine)


def run_restart_check(
    *, database_url: str, modal_environment: str, runtime: RestartRuntime
) -> dict[str, int]:
    """Run one restart check on the run tables; return how each row ended."""
    scheduled = scheduled_restart_service(
        database_url=database_url,
        modal_environment=modal_environment,
        runtime=runtime,
    )
    try:
        return run_scheduled_check(scheduled.service, runtime)
    finally:
        scheduled.engine.dispose()
