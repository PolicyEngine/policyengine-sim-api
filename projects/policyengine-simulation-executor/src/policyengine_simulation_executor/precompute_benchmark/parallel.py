"""Bounded dispatch of independent state/year preparation calls."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Protocol

from policyengine.tax_benefit_models.us.state_preparation import (
    USStatePartition,
    USStateYearArtifact,
)
from pydantic import Field, model_validator

from .profiling import Measurement, StrictModel


class StateYearTask(StrictModel):
    partition: USStatePartition
    year: int = Field(gt=0)


class USStateYearPlan(StrictModel):
    tasks: tuple[StateYearTask, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_tasks(self) -> USStateYearPlan:
        if len({(task.partition.state_code, task.year) for task in self.tasks}) != len(
            self.tasks
        ):
            raise ValueError("State/year preparation tasks must be unique")
        parent = self.tasks[0].partition.parent
        if any(task.partition.parent != parent for task in self.tasks):
            raise ValueError("All preparation tasks must use the same certified source")
        return self


class PreparedTaskResult(StrictModel):
    artifact: USStateYearArtifact
    measurement: Measurement


class USPreparationResult(StrictModel):
    tasks: tuple[PreparedTaskResult, ...]


class StateYearCall(Protocol):
    def result(self) -> PreparedTaskResult: ...


class StateYearWorker(Protocol):
    def spawn(self, task: StateYearTask) -> StateYearCall: ...


class JSONCall(Protocol):
    def get(self) -> str: ...


class JSONFunction(Protocol):
    def spawn(self, payload: str) -> JSONCall: ...


class _ModalCall:
    def __init__(self, call: JSONCall):
        self.call = call

    def result(self) -> PreparedTaskResult:
        return PreparedTaskResult.model_validate_json(self.call.get())


class ModalStateYearWorker:
    """Validate JSON at the Modal boundary; keep internal calls strongly typed."""

    def __init__(self, function: JSONFunction):
        self.function = function

    def spawn(self, task: StateYearTask) -> StateYearCall:
        return _ModalCall(self.function.spawn(task.model_dump_json()))


def _run_task(task: StateYearTask, worker: StateYearWorker) -> PreparedTaskResult:
    result = worker.spawn(task).result()
    if result.artifact.partition != task.partition or result.artifact.year != task.year:
        raise ValueError("Worker returned an artifact for a different preparation task")
    if result.measurement.status != "completed":
        raise ValueError("Worker returned failed benchmark measurements")
    return result


def prepare_state_years_parallel(
    plan: USStateYearPlan, worker: StateYearWorker, *, max_workers: int = 10
) -> USPreparationResult:
    """Keep at most ten remote preparations in flight, without automatic retries.

    A completed task releases one slot immediately, rather than waiting for the
    slowest task in a batch. On failure, stop submitting new work and await the
    already-started calls, preserving their evidence. No DataFrames cross Modal.
    """
    if (
        isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or not 1 <= max_workers <= 10
    ):
        raise ValueError("max_workers must be between 1 and 10")
    results: dict[int, PreparedTaskResult] = {}
    entries = iter(enumerate(plan.tasks))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending: dict[Future[PreparedTaskResult], int] = {}
        for index, task in entries:
            pending[executor.submit(_run_task, task, worker)] = index
            if len(pending) == max_workers:
                break
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            # Validate all completions before dispatching another task.
            for future in done:
                index = pending.pop(future)
                results[index] = future.result()
            for _ in done:
                entry = next(entries, None)
                if entry is not None:
                    index, task = entry
                    pending[executor.submit(_run_task, task, worker)] = index
    return USPreparationResult(
        tasks=tuple(results[index] for index in range(len(plan.tasks)))
    )
