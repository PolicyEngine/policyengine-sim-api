"""Independent Modal orchestration for one Stage 12 segmented simulation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import logging
import time
from typing import Any

import modal
from policyengine_observability import ObservabilityRuntime
from policyengine_simulation_contract.stage12_execution import (
    PlannedSimulationExecutionInput,
)
from policyengine_simulation_observability.stages import (
    STAGE12_SIMULATION_STAGES,
    Stage,
)

from .partition import stage12_region_groups_for_model
from .segmentation import build_segment_inputs, merge_segment_results
from .simulation import SimulationCalculation

logger = logging.getLogger(__name__)

SEGMENT_FUNCTION_NAME = "run_single_simulation_segment_us"
POLL_INTERVAL_INITIAL_SECONDS = 0.5
POLL_INTERVAL_MAX_SECONDS = 8.0
POLL_INTERVAL_BACKOFF_FACTOR = 2.0


def _next_backoff(current: float, *, maximum: float) -> float:
    return min(current * POLL_INTERVAL_BACKOFF_FACTOR, maximum)


class Stage12SegmentRunner:
    """Start, collect, and merge the 20 parts of one logical simulation."""

    def __init__(
        self,
        simulation: PlannedSimulationExecutionInput,
        *,
        app_name: str,
        runtime: ObservabilityRuntime,
        modal_module: Any = modal,
        groups: Sequence[Sequence[str]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = POLL_INTERVAL_INITIAL_SECONDS,
        poll_interval_max_seconds: float = POLL_INTERVAL_MAX_SECONDS,
    ) -> None:
        self.simulation = simulation
        self.runtime = runtime
        self.sleep = sleep
        self.poll_interval_initial_seconds = poll_interval_seconds
        self.poll_interval_max_seconds = poll_interval_max_seconds
        self.groups = [list(group) for group in groups] if groups is not None else None
        self.child_function = modal_module.Function.from_name(
            app_name,
            SEGMENT_FUNCTION_NAME,
        )

    def _groups(self) -> list[list[str]]:
        if self.groups is not None:
            return self.groups
        from policyengine_simulation_executor.simulation_runtime import (
            _country_module,
        )

        country = self.simulation.geography.country
        return stage12_region_groups_for_model(
            country,
            _country_module(country).model,
        )

    def run(self) -> SimulationCalculation:
        groups = self._groups()
        segments = build_segment_inputs(self.simulation, groups=groups)
        self.runtime.set_context(
            stage12_execution="segmented",
            stage12_segment_count=len(segments),
        )
        handles: list[tuple[int, Any]] = []
        try:
            for segment in segments:
                call = None
                with self.runtime.span(
                    STAGE12_SIMULATION_STAGES.name(Stage.STAGE12_SEGMENT_DISPATCH),
                    attributes={"segment_index": segment.segment_index},
                ):
                    call = self.child_function.spawn(
                        segment.model_dump(mode="python"),
                        observability_context=self.runtime.capture_context(),
                    )
                if call is None:
                    raise RuntimeError("Stage 12 segment dispatch returned no call")
                handles.append((segment.segment_index, call))
        except Exception:
            self._cancel_all(handles)
            raise

        payloads: list[object] | None = None
        with self.runtime.span(
            STAGE12_SIMULATION_STAGES.name(Stage.STAGE12_SEGMENT_WAIT)
        ):
            payloads = self._collect(handles)
        if payloads is None:
            raise RuntimeError("Stage 12 segment wait returned no payloads")
        merged: SimulationCalculation | None = None
        with self.runtime.span(
            STAGE12_SIMULATION_STAGES.name(Stage.STAGE12_SEGMENT_MERGE)
        ):
            merged = merge_segment_results(
                self.simulation,
                payloads,
                groups=groups,
            )
        if merged is None:
            raise RuntimeError("Stage 12 segment merge returned no calculation")
        return merged

    def _collect(self, handles: list[tuple[int, Any]]) -> list[object]:
        results: list[object | None] = [None] * len(handles)
        pending = set(range(len(handles)))
        poll_errors: dict[int, int] = {}
        current_sleep = self.poll_interval_initial_seconds
        poll_count = 0

        while pending:
            progress_made = False
            for index in sorted(pending):
                segment_index, call = handles[index]
                poll_count += 1
                try:
                    results[index] = call.get(timeout=0)
                except TimeoutError:
                    poll_errors.pop(index, None)
                    continue
                except Exception as error:
                    attempts = poll_errors.get(index, 0) + 1
                    poll_errors[index] = attempts
                    if attempts < 2:
                        logger.warning(
                            "Stage 12 segment %d poll failed once; retrying (%s)",
                            segment_index,
                            type(error).__name__,
                        )
                        continue
                    self._cancel_all(handles)
                    raise RuntimeError(
                        f"Stage 12 segment {segment_index} failed"
                    ) from None
                pending.discard(index)
                poll_errors.pop(index, None)
                progress_made = True
            self.runtime.set_context(stage12_segment_poll_count=poll_count)
            if pending and not progress_made:
                self.sleep(current_sleep)
                current_sleep = _next_backoff(
                    current_sleep,
                    maximum=self.poll_interval_max_seconds,
                )
            elif progress_made:
                current_sleep = self.poll_interval_initial_seconds

        if any(result is None for result in results):
            raise RuntimeError("Stage 12 segment collection is incomplete")
        return [result for result in results if result is not None]

    @staticmethod
    def _cancel_all(handles: list[tuple[int, Any]]) -> None:
        for _, call in handles:
            try:
                call.cancel()
            except Exception:
                pass


def run_segmented_simulation(
    simulation: PlannedSimulationExecutionInput,
    *,
    app_name: str,
    runtime: ObservabilityRuntime,
) -> SimulationCalculation:
    return Stage12SegmentRunner(
        simulation,
        app_name=app_name,
        runtime=runtime,
    ).run()
