"""Simulation runs another call holds, for tests of a coordinator that waits for them.

Another report run's worker, or an earlier attempt's worker of the same report
run, may hold a simulation run a report run names. ``HolderCall`` stands in for
that call: it runs for a few probes, then records the run's output, records its
failure, or ends without recording anything, as a worker whose container
stopped. ``HolderInvoker`` restores such calls by their call IDs and starts the
coordinator's own workers as the real worker.
"""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING

from policyengine_simulation_executor.stage12_artifacts import (
    serialize_simulation_frames,
)
from stage12_fake_stores import simulation_frames
from stage12_run_worker_support import RunWorkerInvoker

if TYPE_CHECKING:
    from uuid import UUID

    from stage12_fake_report_runs import FakeReportRuns

OTHER_WORKER_FAILURE = "simulation_execution_failed: the other worker's calculation"


class HolderEnding(StrEnum):
    """How a holder call ends."""

    SUCCEEDS = "succeeds"
    FAILS = "fails"
    # The call ends without recording the run's outcome, as when its
    # container runs out of memory.
    STOPS = "stops"


class StoredOutputFiles:
    """Stored output files by URI, recording each read.

    A URI this holds no file for is read from ``fallback``, such as the
    artifact store the coordinator's own workers write to, when one is given.
    """

    def __init__(self, fallback=None) -> None:
        self.payloads: dict[str, bytes] = {}
        self.reads: list[str] = []
        self.fallback = fallback

    def read(self, uri: str) -> bytes:
        self.reads.append(uri)
        if uri in self.payloads:
            return self.payloads[uri]
        if self.fallback is not None:
            return self.fallback.read(uri)
        raise FileNotFoundError(uri)


def stored_output_uri(simulation_run_id: UUID) -> str:
    """Return where a holder call stores its run's output file."""
    return f"gs://detailed-save-outputs/simulations/{simulation_run_id}.parquet"


class HolderCall:
    """A call holding a simulation run, which ends after ``probes_before_ending``.

    Until then each probe raises ``TimeoutError``, as Modal's ``get`` does for a
    call still running.
    """

    def __init__(  # noqa: PLR0913  # the call, its run and stores, and how it ends
        self,
        object_id: str,
        *,
        report_runs: FakeReportRuns,
        simulation_run_id: UUID,
        files: StoredOutputFiles,
        ending: HolderEnding,
        probes_before_ending: int = 2,
    ) -> None:
        self.object_id = object_id
        self.report_runs = report_runs
        self.simulation_run_id = simulation_run_id
        self.files = files
        self.ending = ending
        self.probes_before_ending = probes_before_ending
        self.probes = 0
        self.cancelled = 0
        # Whether the call has ended and recorded what its ending records.
        self.ended = False

    def get(self, *, timeout=None):
        self.probes += 1
        if self.probes <= self.probes_before_ending:
            msg = "the holder call is still running"
            raise TimeoutError(msg)
        first_ending = not self.ended
        self.ended = True
        if self.ending is HolderEnding.SUCCEEDS:
            if first_ending:
                self._record_output()
            return {"evaluation_id": "another report run's"}
        if self.ending is HolderEnding.FAILS:
            if first_ending:
                self.report_runs.fail_simulation_run(
                    self.simulation_run_id,
                    modal_call_id=self.object_id,
                    error_message=OTHER_WORKER_FAILURE,
                    failed_at=None,
                )
            msg = "Stage 12 simulation execution failed"
            raise RuntimeError(msg)
        msg = "the holder call's container stopped"
        raise RuntimeError(msg)

    def cancel(self) -> None:
        self.cancelled += 1

    def _record_output(self) -> None:
        payload, _row_identity = serialize_simulation_frames(simulation_frames(90.0))
        uri = stored_output_uri(self.simulation_run_id)
        self.files.payloads[uri] = payload
        self.report_runs.complete_simulation_run(
            self.simulation_run_id,
            modal_call_id=self.object_id,
            output_uri=uri,
            output_sha256=sha256(payload).hexdigest(),
            completed_at=None,
        )


class HolderInvoker(RunWorkerInvoker):
    """Restore each holder call by its ID; start other children as real workers."""

    def __init__(self, report_runs, artifacts, *, holders=(), **options) -> None:
        super().__init__(report_runs, artifacts, **options)
        self.holders = {holder.object_id: holder for holder in holders}

    def restore(self, invocation_id: str):
        if invocation_id in self.holders:
            self.restored[invocation_id] = self.holders[invocation_id]
            return self.holders[invocation_id]
        return super().restore(invocation_id)
