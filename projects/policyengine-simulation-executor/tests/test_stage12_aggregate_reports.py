"""Tests for the aggregate report a Stage 12 coordinator writes."""

from __future__ import annotations

import json
from hashlib import sha256
from uuid import uuid4

import pandas as pd
import pytest
from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.stage12_execution import (
    ReportExecutionInput,
    SimulationRole,
)
from policyengine_simulation_executor.stage12_runtime import (
    RetainedSimulationOutput,
    SimulationWorkerDependencies,
    _build_spm_result,
    build_aggregate_report,
    run_single_simulation,
    simulation_input_sha256,
)
from stage12_fake_stores import FakeArtifacts, FakeStore, simulation_frames
from stage12_runtime_support import (
    EVALUATION_ID,
    bundle_provenance,
    invocation_context,
    planned_simulation,
    report_execution_input,
    simulation_execution_input,
    spm_calculation_provenance,
)


def test_aggregate_combines_detached_spm_receipts() -> None:
    provenance = spm_calculation_provenance()

    report = report_execution_input()
    report = report.model_copy(
        update={
            "baseline": report.baseline.model_copy(
                update={"options": {"spm": {"scenario": "official"}}}
            ),
            "reform": report.reform.model_copy(
                update={"options": {"spm": {"scenario": "official"}}}
            ),
        }
    )
    baseline = (
        FakeArtifacts()
        .add_simulation(report.baseline)
        .model_copy(update={"calculation_provenance": provenance})
    )
    reform = (
        FakeArtifacts()
        .add_simulation(report.reform)
        .model_copy(update={"calculation_provenance": provenance})
    )

    result = _build_spm_result(
        report=report,
        baseline_descriptor=baseline,
        reform_descriptor=reform,
    )

    assert "spm_config" not in result
    assert result["spm_provenance"]["baseline"]["execution_count"] == 1
    assert result["spm_provenance"]["reform"]["execution_count"] == 1
    assert result["spm_provenance"]["baseline"]["receipt"]["scenario"] == "official"


def test_aggregate_stand_ins_preserve_policy_and_cliff_options(monkeypatch) -> None:
    from policyengine.outputs import labor_supply_response_is_active
    from policyengine_simulation_executor import simulation_output_builder

    report = report_execution_input()
    options = {"include_cliffs": True}
    report = report.model_copy(
        update={
            "baseline": report.baseline.model_copy(update={"options": options}),
            "reform": report.reform.model_copy(
                update={
                    "options": options,
                    "policy": {
                        "gov.simulation.labor_supply_responses.elasticities.income": 0.1
                    },
                }
            ),
        }
    )
    frames = {
        entity: pd.DataFrame({f"{entity}_id": [1]})
        for entity in (
            "person",
            "marital_unit",
            "family",
            "spm_unit",
            "tax_unit",
            "household",
        )
    }
    observed = {}

    class CapturingBuilder:
        def __init__(self, **values):
            observed.update(values)

        def serialize(self):
            return {"captured": True}

    monkeypatch.setattr(
        simulation_output_builder,
        "SimulationOutputBuilder",
        CapturingBuilder,
    )
    artifacts = FakeArtifacts()

    result = build_aggregate_report(
        report=report,
        baseline=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.baseline)
        ),
        reform=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.reform)
        ),
    )

    assert result["result"] == {"captured": True}
    assert observed["simulation_params"]["include_cliffs"] is True
    assert observed["uk_local_authority_metadata"] is None
    assert labor_supply_response_is_active(
        observed["baseline"],
        observed["reform"],
        country_code="us",
    )


def test_aggregate_stand_ins_keep_bounded_policy_end_dates(monkeypatch) -> None:
    from datetime import datetime

    from policyengine_simulation_executor import simulation_output_builder

    path = "gov.simulation.labor_supply_responses.elasticities.income"
    report = report_execution_input()
    report = report.model_copy(
        update={
            "baseline": report.baseline.model_copy(
                update={"policy": {path: {"2026-01-01.2026-12-31": 0.05}}}
            ),
            "reform": report.reform.model_copy(
                update={"policy": {path: {"2026-01-01.2027-12-31": 0.1}}}
            ),
        }
    )
    frames = {
        entity: pd.DataFrame({f"{entity}_id": [1]})
        for entity in (
            "person",
            "marital_unit",
            "family",
            "spm_unit",
            "tax_unit",
            "household",
        )
    }
    observed = {}

    class CapturingBuilder:
        def __init__(self, **values):
            observed.update(values)

        def serialize(self):
            return {"captured": True}

    monkeypatch.setattr(
        simulation_output_builder, "SimulationOutputBuilder", CapturingBuilder
    )
    artifacts = FakeArtifacts()

    build_aggregate_report(
        report=report,
        baseline=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.baseline)
        ),
        reform=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.reform)
        ),
    )

    for role, stop in (("baseline", "2026-12-31"), ("reform", "2027-12-31")):
        (value,) = observed[role].policy.parameter_values
        assert value.end_date == datetime.strptime(stop, "%Y-%m-%d"), role


def _uk_report() -> ReportExecutionInput:
    uk_bundle = bundle_provenance().model_copy(
        update={
            "country_package_name": "policyengine-uk",
            "country_package_version": "2.102.3",
        }
    )
    report_payload = report_execution_input().model_dump(mode="json")
    for role, policy in (
        ("baseline", {}),
        (
            "reform",
            {
                "gov.hmrc.income_tax.charges.CB_HITC.phase_out_start": {
                    "2026-01-01.2100-12-31": 70_000
                }
            },
        ),
    ):
        report_payload[role].update(
            {
                "policy": policy,
                "geography": {"country": "uk", "region": "uk"},
                "bundle": uk_bundle.model_dump(mode="json"),
            }
        )
    return ReportExecutionInput.model_validate(report_payload)


def test_uk_aggregate_without_spm_provenance_is_written(monkeypatch) -> None:
    from policyengine_simulation_executor import simulation_output_builder

    report = _uk_report()
    assert report.baseline.options.get("spm") is None
    uk_output = {
        **{
            key: value
            for key, value in CURRENT_SINGLE_YEAR_MACRO_RESULT.items()
            if key != "spm_provenance"
        },
        "congressional_district_impact": None,
    }

    class UKOutputBuilder:
        def __init__(self, **_values):
            pass

        def serialize(self):
            # The real builder dumps SingleYearMacroOutput, which serializes
            # the unset optional SPM receipt as an explicit null.
            return SingleYearMacroOutput.model_validate(uk_output).model_dump(
                mode="json"
            )

    monkeypatch.setattr(
        simulation_output_builder, "SimulationOutputBuilder", UKOutputBuilder
    )
    uk_frames = {
        entity: pd.DataFrame({f"{entity}_id": [1]})
        for entity in ("person", "benunit", "household")
    }
    artifacts = FakeArtifacts()

    aggregate = build_aggregate_report(
        report=report,
        baseline=RetainedSimulationOutput(
            uk_frames, artifacts.add_simulation(report.baseline)
        ),
        reform=RetainedSimulationOutput(
            uk_frames, artifacts.add_simulation(report.reform)
        ),
    )
    assert aggregate["result"]["spm_provenance"] is None

    aggregate_reference = artifacts.write_aggregate(
        prefix=invocation_context().artifact_prefix, payload=aggregate
    )

    stored_aggregate = artifacts.read(aggregate_reference.uri)
    assert aggregate_reference.content_sha256 == sha256(stored_aggregate).hexdigest()
    assert json.loads(stored_aggregate)["result"]["spm_provenance"] is None


@pytest.mark.parametrize(
    ("region", "routed", "omitted"),
    [
        ("uk", True, True),
        ("country/wales", True, True),
        ("uk", False, False),
        ("constituency/E14001063", True, False),
    ],
)
def test_uk_aggregate_omits_area_breakdowns_only_for_routed_national_reports(
    monkeypatch, region: str, *, routed: bool, omitted: bool
) -> None:
    from policyengine_simulation_executor import simulation_output_builder

    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "uk_area_regions_routed",
        lambda: routed,
    )
    observed = {}

    class CapturingBuilder:
        def __init__(self, **values):
            observed.update(values)

        def serialize(self):
            return {}

    monkeypatch.setattr(
        simulation_output_builder, "SimulationOutputBuilder", CapturingBuilder
    )
    payload = _uk_report().model_dump(mode="json")
    for role in ("baseline", "reform"):
        payload[role]["geography"] = {"country": "uk", "region": region}
    report = ReportExecutionInput.model_validate(payload)
    frames = {
        entity: pd.DataFrame({f"{entity}_id": [1]})
        for entity in ("person", "benunit", "household")
    }
    artifacts = FakeArtifacts()

    build_aggregate_report(
        report=report,
        baseline=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.baseline)
        ),
        reform=RetainedSimulationOutput(
            frames, artifacts.add_simulation(report.reform)
        ),
    )

    assert observed["omit_uk_area_breakdowns"] is omitted
    # The runtime omits them from exactly the same runs.
    from policyengine_simulation_executor import uk_local_authority_metadata

    assert uk_local_authority_metadata.uk_area_breakdowns_omitted("uk", region) is (
        omitted
    )


def test_aggregate_report_wraps_builder_output_with_report_provenance(
    monkeypatch,
) -> None:
    from policyengine_simulation_executor import simulation_output_builder

    report = report_execution_input()
    report = report.model_copy(
        update={"reform": report.reform.model_copy(update={"policy": {}})}
    )
    entities = ("person", "marital_unit", "family", "spm_unit", "tax_unit")

    def frames(income):
        return {
            **{entity: pd.DataFrame({f"{entity}_id": [1]}) for entity in entities},
            "household": pd.DataFrame(
                {"household_id": [1], "household_net_income": [income]}
            ),
        }

    observed = {}

    class CapturingBuilder:
        def __init__(self, **values):
            observed.update(values)

        def serialize(self):
            return {"budget": {"budgetary_impact": 1.0}}

    monkeypatch.setattr(
        simulation_output_builder, "SimulationOutputBuilder", CapturingBuilder
    )
    artifacts = FakeArtifacts()

    result = build_aggregate_report(
        report=report,
        baseline=RetainedSimulationOutput(
            frames(100.0), artifacts.add_simulation(report.baseline)
        ),
        reform=RetainedSimulationOutput(
            frames(120.0), artifacts.add_simulation(report.reform)
        ),
        uk_local_authority_metadata=None,
    )

    assert result == {
        "contract_version": 1,
        "aggregate_schema_version": 1,
        "evaluation_id": str(EVALUATION_ID),
        "requested_aggregates": [item.value for item in report.requested_aggregates],
        "bundle": report.baseline.bundle.model_dump(mode="json"),
        "result": {"budget": {"budgetary_impact": 1.0}},
    }
    assert list(observed) == [
        "country",
        "simulation_params",
        "country_module",
        "dataset",
        "baseline",
        "reform",
        "resolved_data_version",
        "resolved_region_code",
        "uk_local_authority_metadata",
        "omit_uk_area_breakdowns",
    ]
    assert observed["country"] == "us"
    assert observed["simulation_params"] == {
        "country": "us",
        "scope": "macro",
        "data": report.baseline.bundle.dataset.uri,
        "data_version": report.baseline.bundle.dataset.artifact_revision,
        "time_period": str(report.baseline.year),
        "region": report.baseline.geography.region,
    }
    assert observed["dataset"] is observed["baseline"].output_dataset
    for role, income in (("baseline", 100.0), ("reform", 120.0)):
        household = observed[role].output_dataset.data.household
        assert household["household_net_income"].tolist() == [income]
    assert observed["resolved_data_version"] == (
        report.baseline.bundle.dataset.artifact_revision
    )
    assert observed["resolved_region_code"] == report.baseline.geography.region
    assert observed["uk_local_authority_metadata"] is None
    assert observed["omit_uk_area_breakdowns"] is False


def test_single_worker_rejects_combined_baseline_and_reform_input() -> None:
    payload = planned_simulation(SimulationRole.BASELINE).model_dump(mode="json")
    payload["baseline"] = {}
    payload["reform"] = {}

    with pytest.raises(ValueError, match="cannot contain baseline or reform"):
        run_single_simulation(
            payload,
            invocation_context().model_dump(mode="json"),
            required_country="us",
            dependencies=SimulationWorkerDependencies(
                store=FakeStore(),
                artifacts=FakeArtifacts(),
                calculator=lambda _: simulation_frames(),
            ),
        )


def test_simulation_input_digest_excludes_temporary_record_identifiers() -> None:
    original = simulation_execution_input(SimulationRole.BASELINE)
    recreated = original.model_copy(
        update={
            "evaluation_id": uuid4(),
            "simulation_execution_id": uuid4(),
        }
    )

    assert simulation_input_sha256(original) == simulation_input_sha256(recreated)
