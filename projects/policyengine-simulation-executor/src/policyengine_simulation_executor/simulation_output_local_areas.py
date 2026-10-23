"""Build the result of a UK local-area breakdown run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.job_progress import JobStage
from policyengine_simulation_observability.stages import (
    ANNUAL_IMPACT_STAGES,
    Stage,
)

from policyengine_simulation_executor import simulation_output_geographic
from policyengine_simulation_executor.job_progress import (
    NULL_JOB_PROGRESS,
    JobProgressReporter,
)
from policyengine_simulation_executor.release_bundle import get_country_release_bundle
from policyengine_simulation_executor.simulation_macro_output import (
    ConstituencyImpactRecord,
    GeographicImpactOutput,
    LocalAreaBreakdownOutput,
    LocalAuthorityImpactRecord,
)
from policyengine_simulation_executor.simulation_output_provenance import (
    output_data_version,
    output_model_version,
    output_span,
)

if TYPE_CHECKING:
    from types import ModuleType

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata

    from policyengine_simulation_executor.stage_reporting_simulation import (
        StageReportingSimulation,
    )

# A local-area breakdown is a UK output only.
_COUNTRY = "uk"


def _area_records[AreaRecord: (ConstituencyImpactRecord, LocalAuthorityImpactRecord)](
    output: GeographicImpactOutput, record_type: type[AreaRecord]
) -> list[AreaRecord]:
    """Return one area level's records, refusing a record of the other level."""
    records: list[AreaRecord] = []
    for record in output.root:
        if not isinstance(record, record_type):
            msg = f"A {record_type.__name__} list holds a {type(record).__name__}"
            raise TypeError(msg)
        records.append(record)
    return records


@dataclass(frozen=True)
class LocalAreaBreakdownBuilder:
    """Build a UK local-area breakdown from the local-area dataset's pair.

    ``baseline`` and ``reform`` are built on the dataset the bundle routes UK
    area regions to, ``local_authority_metadata`` is the authority boundary
    version detected on it, and ``dataset_name`` and ``data_version`` are its
    bundle name and the revision the release manifest records for it (None
    falls back to the release's data version, as the macro output does).
    ``year`` labels the disclosure release, and ``country_module`` supplies
    the model version. Each segment is built inside its own observability
    span when a ``runtime`` is given.
    """

    baseline: StageReportingSimulation
    reform: StageReportingSimulation
    local_authority_metadata: UKLocalAuthorityMetadata
    dataset_name: str
    data_version: str | None
    year: int
    country_module: ModuleType
    runtime: ObservabilityRuntime | None = None
    progress: JobProgressReporter = NULL_JOB_PROGRESS

    def build(self) -> LocalAreaBreakdownOutput:
        """Build the UK constituency and local-authority impacts alone.

        No national impact analysis runs. Both simulations are computed first,
        baseline then reform, so a status poll moves through those stages
        before the outputs stage, and both levels then come from one
        disclosure release over the pair.
        """
        self.baseline.ensure()
        self.reform.ensure()
        self.progress.enter(JobStage.OUTPUTS)
        with output_span(
            self.runtime, ANNUAL_IMPACT_STAGES.name(Stage.SIMULATION_OUTPUT_BUILD)
        ):
            with output_span(
                self.runtime, ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_UK_CONSTITUENCY)
            ):
                disclosure = (
                    simulation_output_geographic.compute_uk_geography_disclosure_output(
                        _COUNTRY, self.baseline, self.reform, year=self.year
                    )
                )
                constituency_impact = (
                    simulation_output_geographic.build_uk_constituency_impact(
                        _COUNTRY, disclosure
                    )
                )
            with output_span(
                self.runtime, ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_UK_LOCAL_AUTHORITY)
            ):
                local_authority_impact = (
                    simulation_output_geographic.build_uk_local_authority_impact(
                        _COUNTRY,
                        disclosure,
                        uk_local_authority_metadata=self.local_authority_metadata,
                    )
                )
            if constituency_impact is None or local_authority_impact is None:
                msg = "UK area outputs returned no constituency or local-authority list"
                raise ValueError(msg)
            return LocalAreaBreakdownOutput(
                kind="local_areas",
                model_version=output_model_version(
                    self.country_module, runtime=self.runtime
                ),
                policyengine_version=(
                    get_country_release_bundle(_COUNTRY).policyengine_version
                ),
                dataset=self.dataset_name,
                data_version=output_data_version(
                    _COUNTRY, runtime=self.runtime, resolved=self.data_version
                ),
                constituency_impact=_area_records(
                    constituency_impact, ConstituencyImpactRecord
                ),
                local_authority_impact=_area_records(
                    local_authority_impact, LocalAuthorityImpactRecord
                ),
            )

    def serialize(self) -> dict[str, Any]:
        """Build the breakdown and dump it to JSON-compatible data."""
        with output_span(self.runtime, ANNUAL_IMPACT_STAGES.name(Stage.CALCULATION)):
            output = self.build()
        with (
            output_span(
                self.runtime, ANNUAL_IMPACT_STAGES.name(Stage.RESPONSE_SERIALIZATION)
            ),
            output_span(
                self.runtime,
                ANNUAL_IMPACT_STAGES.name(Stage.SIMULATION_OUTPUT_MODEL_DUMP),
            ),
        ):
            return output.model_dump(mode="json")
