"""Tests for the area results of a UK-wide report on the local-area dataset."""

from __future__ import annotations

import copy
from typing import Any

import pandas as pd
import pytest
from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactPayload,
    ReportExecutionInput,
)
from policyengine_simulation_executor import (
    simulation_output_builder,
    simulation_output_geographic,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)
from policyengine_simulation_executor.simulation_output_geographic import (
    UKAreaIncomeChange,
    UKGeographyDisclosureOutput,
)
from policyengine_simulation_executor.stage12_runtime import (
    RetainedSimulationOutput,
    build_aggregate_report,
    local_area_datasets,
)
from policyengine_stage12_persistence.report_run_results import (
    report_run_result,
    withheld_area_codes,
)
from stage12_fake_report_runs import REPORT_RUN_IDS
from stage12_fake_stores import FakeArtifacts
from stage12_runtime_support import bundle_provenance, report_execution_input

NATIONAL_DATASET = "microcosm_uk_2024_25"
LOCAL_AREA_DATASET = "microcosm_uk_2024_25_dense"
UK_REFORM = {
    "gov.hmrc.income_tax.charges.CB_HITC.phase_out_start": {
        "2026-01-01.2100-12-31": 70_000
    }
}
PUBLISHED = UKAreaIncomeChange(
    tier="reported",
    average=-10.0,
    average_lower=-12.0,
    average_upper=-8.0,
    relative=-0.0003,
    relative_lower=-0.0004,
    relative_upper=-0.0002,
    population=41_000.0,
)
# Two constituencies and two authorities estimated; one of each published.
DISCLOSURE = UKGeographyDisclosureOutput(
    areas={
        "constituency": {"E14001063": PUBLISHED},
        "local_authority": {"E06000063": PUBLISHED},
    },
    estimated={
        "constituency": frozenset({"E14001063", "W07000081"}),
        "local_authority": frozenset({"E06000063", "W06000015", "E06000064"}),
    },
)


def _route_uk_area_regions(monkeypatch) -> None:
    """Make the installed release route UK area regions to the local-area dataset."""
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "uk_area_regions_routed",
        lambda: True,
    )
    monkeypatch.setattr(local_area_datasets, "uk_area_regions_routed", lambda: True)
    monkeypatch.setattr(
        local_area_datasets, "routed_uk_area_dataset_name", lambda: LOCAL_AREA_DATASET
    )


def _uk_report(*, region: str, dataset: str) -> ReportExecutionInput:
    """Return a UK report whose two simulations are on ``dataset``."""
    provenance = bundle_provenance()
    bundle = provenance.model_copy(
        update={
            "country_package_name": "policyengine-uk",
            "country_package_version": "2.174.1",
            "dataset": provenance.dataset.model_copy(
                update={
                    "identity": dataset,
                    "uri": f"hf://policyengine/microcosm-uk/{dataset}.h5@revision",
                }
            ),
        }
    )
    payload = report_execution_input().model_dump(mode="json")
    for role, policy in (("baseline", {}), ("reform", UK_REFORM)):
        payload[role].update(
            {
                "policy": policy,
                "geography": {"country": "uk", "region": region},
                "bundle": bundle.model_dump(mode="json"),
                "population": {
                    "kind": "dataset",
                    "artifact": {
                        "uri": bundle.dataset.uri,
                        "media_type": "application/x-hdf5",
                        "content_sha256": "a" * 64,
                    },
                },
            }
        )
    return ReportExecutionInput.model_validate(payload)


class _CapturingBuilder:
    """Stands in for the output builder: records its fields, lists codes."""

    built: dict[str, Any]

    def __init__(self, **values: Any) -> None:
        type(self).built = values

    def serialize(self) -> dict[str, Any]:
        return {"constituency_impact": [], "local_authority_impact": []}

    def uk_withheld_area_codes(self) -> dict[str, list[str]]:
        return {
            "constituency": ["W07000081"],
            "local_authority": ["E06000064", "W06000015"],
        }


def _aggregate(monkeypatch, report: ReportExecutionInput) -> dict[str, Any]:
    monkeypatch.setattr(
        simulation_output_builder, "SimulationOutputBuilder", _CapturingBuilder
    )
    frames = {
        entity: pd.DataFrame({f"{entity}_id": [1]})
        for entity in ("person", "benunit", "household")
    }
    artifacts = FakeArtifacts()
    return build_aggregate_report(
        report=report,
        baseline=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.baseline)
        ),
        reform=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.reform)
        ),
    )["result"]


@pytest.mark.parametrize("region", ["uk", "country/wales"])
def test_a_uk_wide_or_nation_report_on_the_local_area_dataset_gets_its_areas(
    monkeypatch, region: str
) -> None:
    _route_uk_area_regions(monkeypatch)

    result = _aggregate(
        monkeypatch, _uk_report(region=region, dataset=LOCAL_AREA_DATASET)
    )

    assert _CapturingBuilder.built["omit_uk_area_breakdowns"] is False
    assert result["constituency_withheld_codes"] == ["W07000081"]
    assert result["local_authority_withheld_codes"] == ["E06000064", "W06000015"]


def test_a_uk_wide_report_on_the_national_dataset_still_leaves_its_areas_out(
    monkeypatch,
) -> None:
    _route_uk_area_regions(monkeypatch)

    result = _aggregate(monkeypatch, _uk_report(region="uk", dataset=NATIONAL_DATASET))

    assert _CapturingBuilder.built["omit_uk_area_breakdowns"] is True
    assert "constituency_withheld_codes" not in result
    assert "local_authority_withheld_codes" not in result


@pytest.mark.parametrize(
    "region", ["constituency/E14001063", "local_authority/E06000063"]
)
def test_an_area_report_keeps_its_area_and_lists_no_withheld_codes(
    monkeypatch, region: str
) -> None:
    # Its result, and its comparison with production, stay as they are.
    _route_uk_area_regions(monkeypatch)

    result = _aggregate(
        monkeypatch, _uk_report(region=region, dataset=LOCAL_AREA_DATASET)
    )

    assert _CapturingBuilder.built["omit_uk_area_breakdowns"] is False
    assert "constituency_withheld_codes" not in result


def test_the_builder_lists_each_levels_withheld_codes_from_one_disclosure_pass(
    monkeypatch,
) -> None:
    passes = []

    def disclose(*_args, **_kwargs) -> UKGeographyDisclosureOutput:
        passes.append(1)
        return DISCLOSURE

    monkeypatch.setattr(
        simulation_output_geographic, "compute_uk_geography_disclosure_output", disclose
    )
    builder = SimulationOutputBuilder(
        country="uk",
        simulation_params={"time_period": "2026"},
        country_module=None,
        dataset=None,
        baseline=None,
        reform=None,
    )

    assert builder.uk_withheld_area_codes() == {
        "constituency": ["W07000081"],
        "local_authority": ["E06000064", "W06000015"],
    }
    # The codes come from the disclosure pass the area results use.
    builder.uk_withheld_area_codes()
    assert len(passes) == 1


def test_a_builder_outside_the_uk_lists_no_withheld_codes() -> None:
    builder = SimulationOutputBuilder(
        country="us",
        simulation_params={},
        country_module=None,
        dataset=None,
        baseline=None,
        reform=None,
    )

    assert builder.uk_withheld_area_codes() is None


class _FullOutputBuilder(_CapturingBuilder):
    """Stands in for the output builder with a complete economy output."""

    def serialize(self) -> dict[str, Any]:
        output = copy.deepcopy(CURRENT_SINGLE_YEAR_MACRO_RESULT)
        # A UK result: its areas, and no congressional districts.
        output.update(
            congressional_district_impact=None,
            constituency_impact=[],
            local_authority_impact=[],
        )
        return output


def test_a_local_area_aggregate_is_stored_with_its_withheld_codes(monkeypatch) -> None:
    _route_uk_area_regions(monkeypatch)
    monkeypatch.setattr(
        simulation_output_builder, "SimulationOutputBuilder", _FullOutputBuilder
    )
    report = _uk_report(region="uk", dataset=LOCAL_AREA_DATASET)
    frames = {
        entity: pd.DataFrame({f"{entity}_id": [1]})
        for entity in ("person", "benunit", "household")
    }
    artifacts = FakeArtifacts()
    aggregate = build_aggregate_report(
        report=report,
        baseline=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.baseline)
        ),
        reform=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.reform)
        ),
    )

    # The coordinator validates the aggregate, then stores its result.
    result = AggregateReportArtifactPayload.model_validate(aggregate).result
    stored = report_run_result(result, run_ids=REPORT_RUN_IDS, country="uk")
    (withheld,) = [rows for rows in stored.rows if rows.table is withheld_area_codes]
    assert [(row["area_type"], row["code"]) for row in withheld.rows] == [
        ("constituency", "W07000081"),
        ("local_authority", "E06000064"),
        ("local_authority", "W06000015"),
    ]


class _NoAreaBuilder(_CapturingBuilder):
    """Stands in for an output builder whose disclosure pass estimated no areas."""

    withheld: dict[str, list[str]]

    def uk_withheld_area_codes(self) -> dict[str, list[str]]:
        return type(self).withheld


@pytest.mark.parametrize("level", ["constituency", "local_authority"])
def test_a_local_area_report_that_estimated_no_area_of_a_level_fails(
    monkeypatch, level: str
) -> None:
    # A succeeded run then proves each level's area or withheld-code rows
    # exist, which a staging check reading only the run's status relies on.
    _route_uk_area_regions(monkeypatch)
    _NoAreaBuilder.withheld = {
        "constituency": ["W07000081"],
        "local_authority": ["W06000015"],
        level: [],
    }
    monkeypatch.setattr(
        simulation_output_builder, "SimulationOutputBuilder", _NoAreaBuilder
    )
    report = _uk_report(region="country/wales", dataset=LOCAL_AREA_DATASET)
    frames = {
        entity: pd.DataFrame({f"{entity}_id": [1]})
        for entity in ("person", "benunit", "household")
    }
    artifacts = FakeArtifacts()

    with pytest.raises(ValueError, match=f"estimated no {level} area"):
        build_aggregate_report(
            report=report,
            baseline=RetainedSimulationOutput(
                frames, artifacts.add_simulation(report.baseline)
            ),
            reform=RetainedSimulationOutput(
                frames, artifacts.add_simulation(report.reform)
            ),
        )
