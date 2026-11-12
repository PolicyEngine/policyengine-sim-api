"""Simulation runs a report run reuses: outputs that already succeeded, used as stored.

The simulation service may name, as a report run's baseline or reform, a
simulation run that already succeeded for the same simulation, year and
versions. The coordinator never claims, starts or calculates that run again.
While it prepares the report (or, for a run a pre-empted attempt's worker
finished, when it restarts it), it reads the run's status, year, versions,
dataset, region, output URI and digest from the database, and checks them
against the simulation input and the report run's other simulation run. It
reads the file from whichever ``gs://`` bucket the URI names, checks its
SHA-256 digest, decodes it once, and checks the outputs the report plans and
the SPM calculation receipt a worker would write now. It describes the file
as a worker describes the one it writes, and the aggregation uses the
decoded frames in place of a worker's artifact.

No worker runs for a reused run. The coordinator writes nothing to it, so
report runs that reuse it at the same time leave each other unaffected, and
the stuck-run sweep never lists a succeeded run. Of the result rows that
belong to the run itself, its poverty and inequality, a report run inserts
only those the run has none of yet.

A succeeded run whose output can't be used fails the report run with one of
the error codes below. It is never calculated again, since a succeeded
simulation run can't be claimed again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_contract.spm import (
    SPMSelection,
    validate_spm_calculation_provenance,
)
from policyengine_simulation_contract.stage12_execution import (
    ArtifactMediaType,
    ArtifactReference,
    SimulationArtifactDescriptor,
    SimulationRole,
    stage12_output_plan_sha256,
)
from policyengine_simulation_observability.stages import Stage
from policyengine_stage12_persistence import RunStatus

from policyengine_simulation_executor.artifact_store import ArtifactStore
from policyengine_simulation_executor.stage12_artifacts import (
    decode_simulation_output,
    deserialize_calculation_provenance,
    simulation_row_identity,
)

from .failures import Stage12InputError
from .output_planning import validate_output_frames
from .worker_inputs import simulation_runtime_params

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from google.cloud import storage
    from policyengine_simulation_contract.spm import SPMProvenance
    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
        ReportRunIds,
    )
    from policyengine_stage12_persistence import SimulationRunState

    from policyengine_simulation_executor.stage12_artifacts import (
        DecodedSimulationOutput,
    )

    from .output_planning import StageScope

# The run records no output file, or no object is stored where it says.
REUSED_OUTPUT_MISSING = "reused_simulation_output_missing"
# The file can't be read, or isn't a Stage 12 simulation output.
REUSED_OUTPUT_UNREADABLE = "reused_simulation_output_unreadable"
REUSED_OUTPUT_DIGEST_MISMATCH = "reused_simulation_output_digest_mismatch"
# The file lacks an entity or column this report's output plan names.
REUSED_OUTPUT_INCOMPATIBLE = "reused_simulation_output_incompatible"
# The file's SPM calculation receipt isn't the one a worker would write for
# this report run's simulation: another selection, another year, or a receipt
# where none is calculated, or none where one is.
REUSED_SPM_MISMATCH = "reused_simulation_spm_mismatch"
# The run calculated another year, country package version or .py version,
# or names another dataset or region than its report run's other run.
REUSED_RUN_MISMATCH = "reused_simulation_run_mismatch"

STORAGE_OBJECT_URI = re.compile(r"gs://(?P<bucket>[^/]+)/(?P<path>.+)")


class ReusedSimulationOutputError(Stage12InputError):
    """A reused run's output can't be used; its code and summary are safe to record."""


class OutputFileReader(Protocol):
    """Reads a stored output file; ``CloudStorageOutputFiles`` implements it."""

    def read(self, uri: str) -> bytes:
        """Return the file's bytes; raise FileNotFoundError when none is stored."""
        ...


class CloudStorageOutputFiles:
    """Reads output files from whichever Cloud Storage bucket their URI names.

    Unless a client is given, the first read builds one from the worker's
    Google Cloud credentials, and later reads share it.
    """

    def __init__(self, client: storage.Client | None = None) -> None:
        """Read through ``client``, or through a client built on the first read."""
        self._client = client

    def read(self, uri: str) -> bytes:
        """Return the bytes of the object ``uri`` names.

        Raises ValueError for a URI that names no Cloud Storage object, and
        FileNotFoundError when no object is stored there.
        """
        match = STORAGE_OBJECT_URI.fullmatch(uri)
        if match is None:
            msg = "an output file URI must name a Cloud Storage object"
            raise ValueError(msg)
        store = ArtifactStore(match["bucket"], client=self._client)
        payload = store.read_bytes(match["path"])
        self._client = store.client
        if payload is None:
            raise FileNotFoundError(uri)
        return payload


@dataclass(frozen=True)
class ReusedSimulationOutput:
    """A succeeded simulation run's checked output file, read and decoded once.

    ``descriptor`` describes the file for this report as a worker describes
    the file it writes; the aggregation uses ``decoded`` as it is.
    """

    decoded: DecodedSimulationOutput
    descriptor: SimulationArtifactDescriptor


def reused_outputs(
    simulations: Sequence[PlannedSimulationExecutionInput],
    *,
    run_ids: ReportRunIds,
    stored_runs: Mapping[SimulationRole, SimulationRunState],
    files: OutputFileReader | None,
    span: StageScope,
) -> dict[str, ReusedSimulationOutput]:
    """Return, by role, the checked output of each simulation whose run succeeded.

    ``stored_runs`` holds the report run's two simulation runs as the
    coordinator read them. ``files`` reads their output files, by default
    from Cloud Storage, and each read is timed with ``span``. A simulation
    run that is pending, running or failed is left to a worker. Raises
    ``ReusedSimulationOutputError`` for a succeeded run whose output can't
    be used.
    """
    reader = files or CloudStorageOutputFiles()
    reused: dict[str, ReusedSimulationOutput] = {}
    for simulation in simulations:
        role = simulation.role
        stored = stored_runs[role]
        if stored.status is not RunStatus.SUCCEEDED:
            continue
        other_role = (
            SimulationRole.REFORM
            if role is SimulationRole.BASELINE
            else SimulationRole.BASELINE
        )
        _require_shared_population(
            stored,
            stored_runs[other_role],
            named=f"The reused {_run_name(role, run_ids)}",
            other_named=_run_name(other_role, run_ids),
        )
        with span(Stage.STAGE12_ARTIFACT_READ, {"simulation_role": role.value}):
            reused[role.value] = checked_output(
                simulation, run_ids.simulation_run_id(role), stored, files=reader
            )
    return reused


def _run_name(role: SimulationRole, run_ids: ReportRunIds) -> str:
    return f"{role.value} simulation run {run_ids.simulation_run_id(role)}"


def _require_shared_population(
    stored: SimulationRunState,
    other: SimulationRunState,
    *,
    named: str,
    other_named: str,
) -> None:
    """Raise unless the run names the dataset and region of its report run's other run.

    The baseline and reform of one report calculate one population and
    geography. The report's input names them by the bundle's dataset identity
    and a region code, which nothing maps to the Public API's catalog rows,
    so the runs are compared with each other.
    """
    differences = [
        f"{column} {_shown(recorded)} instead of {_shown(shared)}"
        for column, recorded, shared in (
            ("dataset_id", stored.dataset_id, other.dataset_id),
            ("region_id", stored.region_id, other.region_id),
        )
        if recorded != shared
    ]
    if differences:
        raise ReusedSimulationOutputError(
            REUSED_RUN_MISMATCH,
            f"{named} differs from this report run's {other_named}: "
            f"{'; '.join(differences)}.",
        )


def _shown(catalog_id: UUID | None) -> str:
    return "none" if catalog_id is None else str(catalog_id)


def checked_output(
    simulation: PlannedSimulationExecutionInput,
    run_id: UUID,
    stored: SimulationRunState,
    *,
    files: OutputFileReader,
) -> ReusedSimulationOutput:
    """Read and check the output file a succeeded run recorded, for ``simulation``.

    Raises ``ReusedSimulationOutputError`` when the run calculated another
    year or version, or its file is missing, unreadable, differs from its
    digest, lacks an entity or column the report plans, or has an SPM
    calculation receipt other than the one a worker would write now.
    """
    named = f"The reused {simulation.role.value} simulation run {run_id}"
    _require_same_calculation(simulation, stored, named=named)
    uri, digest, payload = _read_output(stored, files=files, named=named)
    try:
        decoded = decode_simulation_output(payload)
        calculation_provenance = deserialize_calculation_provenance(payload)
        row_identity = simulation_row_identity(decoded.frames)
    except Exception as error:
        raise ReusedSimulationOutputError(
            REUSED_OUTPUT_UNREADABLE,
            f"{named} names an output file that is not a readable simulation "
            f"output ({type(error).__name__}).",
        ) from error
    try:
        validate_output_frames(decoded.frames, simulation.output_plan)
    except ValueError as error:
        raise ReusedSimulationOutputError(
            REUSED_OUTPUT_INCOMPATIBLE,
            f"{named} names an output file without outputs this report needs: {error}",
        ) from error
    _require_resolved_spm_calculation(simulation, calculation_provenance, named=named)
    return ReusedSimulationOutput(
        decoded=decoded,
        descriptor=SimulationArtifactDescriptor(
            evaluation_id=simulation.evaluation_id,
            simulation_execution_id=simulation.simulation_execution_id,
            role=simulation.role,
            artifact=ArtifactReference(
                uri=uri,
                media_type=ArtifactMediaType.PARQUET,
                content_sha256=digest,
                size_bytes=len(payload),
            ),
            # The file holds every entity and column the plan names.
            output_plan_sha256=stage12_output_plan_sha256(simulation.output_plan),
            row_identity=row_identity,
            bundle=simulation.bundle,
            calculation_provenance=calculation_provenance,
        ),
    )


def _require_same_calculation(
    simulation: PlannedSimulationExecutionInput,
    stored: SimulationRunState,
    *,
    named: str,
) -> None:
    """Raise unless the run calculated this simulation's year and versions."""
    expected = {
        "year": simulation.year,
        "country_package_version": simulation.bundle.country_package_version,
        "policyengine_version": simulation.bundle.policyengine_version,
    }
    recorded = {
        "year": stored.year,
        "country_package_version": stored.country_package_version,
        "policyengine_version": stored.policyengine_version,
    }
    differences = [
        f"{name} {recorded[name]!r} instead of {expected[name]!r}"
        for name in expected
        if recorded[name] != expected[name]
    ]
    if differences:
        raise ReusedSimulationOutputError(
            REUSED_RUN_MISMATCH,
            f"{named} differs from this report run's simulation: "
            f"{'; '.join(differences)}.",
        )


def _require_resolved_spm_calculation(
    simulation: PlannedSimulationExecutionInput,
    receipt: SPMProvenance | None,
    *,
    named: str,
) -> None:
    """Raise unless the file has the SPM calculation receipt a worker would write now.

    The worker resolves the simulation's SPM request with
    ``normalize_runtime_spm``, so an option the request leaves out takes the
    bundle's certified default, and a US request with no SPM option still
    calculates the default selection. A calculation under another selection
    calculates other values.
    """
    from policyengine_simulation_executor.spm import normalize_runtime_spm

    resolved = normalize_runtime_spm(simulation_runtime_params(simulation))
    if resolved is None and receipt is None:
        return
    if resolved is None:
        raise ReusedSimulationOutputError(
            REUSED_SPM_MISMATCH,
            f"{named} names an output file with an SPM calculation receipt, but "
            "this report run's simulation calculates no SPM measure.",
        )
    if receipt is None:
        raise ReusedSimulationOutputError(
            REUSED_SPM_MISMATCH,
            f"{named} names an output file without an SPM calculation receipt, "
            "but this report run's simulation calculates an SPM measure.",
        )
    try:
        validate_spm_calculation_provenance(
            config=SPMSelection.model_validate(resolved), receipt=receipt
        )
    except ValueError as error:
        raise ReusedSimulationOutputError(
            REUSED_SPM_MISMATCH,
            f"{named} names an output file calculated under another SPM "
            f"selection than this report run's: {error}.",
        ) from error
    if str(simulation.year) not in receipt.years:
        raise ReusedSimulationOutputError(
            REUSED_SPM_MISMATCH,
            f"{named} names an output file whose SPM calculation receipt covers "
            f"{', '.join(receipt.years) or 'no year'}, not {simulation.year}.",
        )


def _read_output(
    stored: SimulationRunState,
    *,
    files: OutputFileReader,
    named: str,
) -> tuple[str, str, bytes]:
    """Return a run's output URI and SHA-256 digest, and the file's checked bytes."""
    if stored.output_uri is None or stored.output_sha256 is None:
        raise ReusedSimulationOutputError(
            REUSED_OUTPUT_MISSING, f"{named} recorded no output file."
        )
    if STORAGE_OBJECT_URI.fullmatch(stored.output_uri) is None:
        raise ReusedSimulationOutputError(
            REUSED_OUTPUT_UNREADABLE,
            f"{named} names an output file that is not a Cloud Storage object.",
        )
    try:
        payload = files.read(stored.output_uri)
    except FileNotFoundError as error:
        raise ReusedSimulationOutputError(
            REUSED_OUTPUT_MISSING, f"{named} names an output file that does not exist."
        ) from error
    except Exception as error:
        raise ReusedSimulationOutputError(
            REUSED_OUTPUT_UNREADABLE,
            f"{named} names an output file that could not be read "
            f"({type(error).__name__}).",
        ) from error
    digest = sha256(payload).hexdigest()
    if digest != stored.output_sha256:
        raise ReusedSimulationOutputError(
            REUSED_OUTPUT_DIGEST_MISMATCH,
            f"{named} names an output file whose SHA-256 digest differs from "
            "the one recorded for it.",
        )
    return stored.output_uri, digest, payload


def simulations_to_calculate(
    simulations: Sequence[PlannedSimulationExecutionInput],
    reused: Mapping[str, ReusedSimulationOutput],
) -> tuple[PlannedSimulationExecutionInput, ...]:
    """Return, in order, the simulations no reused output stands in for."""
    return tuple(
        simulation for simulation in simulations if simulation.role.value not in reused
    )
