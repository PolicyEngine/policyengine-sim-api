"""One v2 economy simulation run's Modal call: calculate, store, describe."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import pandas as pd
import pytest
from fixtures.fake_object_store import InMemoryObjectStore
from policyengine.outputs import labor_supply_response as py_labor_supply
from policyengine_simulation_contract.economy_runs import (
    LABOR_SUPPLY_RESPONSE_PARAMETER_PREFIXES,
    CatalogPopulation,
    CatalogRowFilter,
    EconomySimulationCall,
    EconomySimulationOutput,
)
from policyengine_simulation_contract.report_execution import ReportExecutionError
from policyengine_simulation_contract.stage12_bundle import (
    ResolvedStage12Bundle,
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
)
from policyengine_simulation_contract.stage12_execution import (
    EntityOutputPlan,
    GeographySelection,
    PlannedSimulationExecutionInput,
    ReportAggregate,
    ReportOutputRequirements,
    SimulationExecutionInput,
    Stage12OutputPlan,
    stage12_output_plan_sha256,
)
from policyengine_simulation_executor.economy_runs import simulation_call
from policyengine_simulation_executor.economy_runs.simulation_call import (
    EconomyRunInvocation,
    EconomySimulationDependencies,
    run_economy_simulation,
)
from policyengine_simulation_executor.stage12_artifacts import Stage12ArtifactStore
from policyengine_simulation_executor.stage12_runtime import (
    SimulationWorkerDependencies,
)
from policyengine_simulation_executor.stage12_runtime.catalog_population import (
    CATALOG_POPULATION_OPTION,
)

RUN_ID = UUID("00000000-0000-0000-0000-0000000000a1")
NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
STATES = Stage12Dataset(
    identity="populace_us_2024_acs_local",
    uri="hf://policyengine/data/populace_us_2024_acs_local.h5@states",
    artifact_revision="states",
    sha256="c" * 64,
    repo_type="dataset",
)
NATIONAL = Stage12Dataset(
    identity="populace_us_2024",
    uri="hf://policyengine/data/populace_us_2024.h5@revision",
    artifact_revision="revision",
    sha256="a" * 64,
    repo_type="dataset",
)
INVOCATION = EconomyRunInvocation(
    application_name="policyengine-simulation-v2-6-7-5",
    modal_environment="staging",
    simulation_callable="run_economy_simulation_us",
)


def _installed_bundle() -> ResolvedStage12Bundle:
    return ResolvedStage12Bundle(
        bundle=Stage12BundleManifest(
            policyengine_version="6.7.5",
            policyengine_requirement="policyengine==6.7.5",
            core_package_version="3.0.0",
            core_package_requirement="policyengine-core==3.0.0",
            countries=(
                Stage12CountryBundle(
                    country="us",
                    country_package_name="policyengine-us",
                    country_package_version="1.764.6",
                    country_package_requirement="policyengine-us==1.764.6",
                    data_package_name="populace-data",
                    data_package_version="0.1.0",
                    data_release_version="revision",
                    data_artifact_revision="revision",
                    default_dataset=NATIONAL.identity,
                    default_dataset_uri=NATIONAL.uri,
                    datasets=(NATIONAL, STATES),
                ),
            ),
        ),
        bundle_manifest_sha256="b" * 64,
    )


def _call(**changes: object) -> EconomySimulationCall:
    values: dict[str, object] = {
        "simulation_run_id": RUN_ID,
        "country": "us",
        "year": 2026,
        "policyengine_version": "6.7.5",
        "policy": {"gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01": 3000}},
        "population": CatalogPopulation(
            region_code="state/ca",
            dataset_name=STATES.identity,
            row_filter=CatalogRowFilter(field="state_fips", value="6"),
            state_code="CA",
        ),
        "artifact_environment": "staging",
    }
    values.update(changes)
    return EconomySimulationCall.model_validate(values)


def _plan(_simulation: SimulationExecutionInput, *, labor_supply_response: bool):
    return Stage12OutputPlan(
        country="us",
        requirements=ReportOutputRequirements(
            aggregates=tuple(ReportAggregate),
            include_cliff_impacts=False,
            labor_supply_response_active=labor_supply_response,
        ),
        entities=(
            EntityOutputPlan(
                entity="household",
                materialized_variables=("household_id", "household_net_income"),
            ),
        ),
    )


def _frames(value: float = 100.0) -> dict[str, pd.DataFrame]:
    return {
        "household": pd.DataFrame(
            {"household_id": [1, 2], "household_net_income": [value, 2 * value]}
        )
    }


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(simulation_call, "load_stage12_bundle", _installed_bundle)


def _run(call: EconomySimulationCall, *, objects=None, calculator=None, **seams):
    objects = objects if objects is not None else InMemoryObjectStore()
    calculated: list[PlannedSimulationExecutionInput] = []

    def record_and_calculate(planned):
        calculated.append(planned)
        return (calculator or (lambda _planned: _frames()))(planned)

    result = run_economy_simulation(
        call.model_dump(mode="json"),
        required_country=seams.pop("required_country", "us"),
        invocation=INVOCATION,
        dependencies=EconomySimulationDependencies(
            worker=SimulationWorkerDependencies(
                artifacts=Stage12ArtifactStore("private", store=objects),
                calculator=record_and_calculate,
            ),
            output_planner=_plan,
            clock=lambda: NOW,
        ),
    )
    return result, calculated, objects


def test_a_call_calculates_its_catalog_population_and_names_what_it_stored(
    installed,
) -> None:
    call = _call()

    result, calculated, objects = _run(call)

    output = EconomySimulationOutput.model_validate(result)
    planned = calculated[0]
    assert planned.geography == GeographySelection(country="us", region="state/ca")
    assert planned.options == {
        CATALOG_POPULATION_OPTION: call.population.model_dump(mode="json")
    }
    assert planned.policy == call.policy
    assert planned.population.artifact.uri == STATES.uri
    assert output.simulation_run_id == RUN_ID
    assert output.bundle.dataset.identity == STATES.identity
    assert output.output_plan_sha256 == stage12_output_plan_sha256(
        _plan(planned, labor_supply_response=False)
    )
    prefix = f"stage-12-runs/staging/2026/10/{RUN_ID}"
    for stored in (output.input, output.output):
        assert stored.uri.startswith(f"gs://private/{prefix}/")
        key = stored.uri.removeprefix("gs://private/")
        assert sha256(objects.objects[key]).hexdigest() == stored.content_sha256
        assert len(objects.objects[key]) == stored.size_bytes


def test_a_call_asks_for_labour_supply_columns_when_its_report_needs_them(
    installed,
) -> None:
    result, calculated, _objects = _run(_call(labor_supply_response=True))

    output = EconomySimulationOutput.model_validate(result)
    assert calculated[0].output_plan.requirements.labor_supply_response_active
    assert output.output_plan_sha256 == stage12_output_plan_sha256(
        _plan(calculated[0], labor_supply_response=True)
    )


def test_a_restarted_call_stores_output_that_differs_in_its_last_bits(
    installed,
) -> None:
    objects = InMemoryObjectStore()
    call = _call()

    first, _calculated, _objects = _run(call, objects=objects)
    restarted, _calculated, _objects = _run(
        call,
        objects=objects,
        calculator=lambda _planned: _frames(math.nextafter(100.0, math.inf)),
    )

    assert first["output"]["uri"] != restarted["output"]["uri"]
    assert first["input"] == restarted["input"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"policyengine_version": "6.8.0"}, "installs PolicyEngine.py 6.7.5"),
        (
            {
                "population": CatalogPopulation(
                    region_code="state/ca",
                    dataset_name="populace_us_2023",
                    row_filter=CatalogRowFilter(field="state_fips", value="6"),
                )
            },
            "no dataset 'populace_us_2023'",
        ),
    ],
)
def test_a_call_this_application_cannot_run_is_refused_before_anything_is_stored(
    installed, changes, message
) -> None:
    objects = InMemoryObjectStore()

    with pytest.raises(ValueError, match=message):
        _run(_call(**changes), objects=objects)

    assert objects.objects == {}


def test_a_call_for_another_country_is_refused(installed) -> None:
    with pytest.raises(ValueError, match="another country"):
        _run(_call(), required_country="uk")


def test_a_failed_calculation_reaches_the_caller_as_a_typed_error(installed) -> None:
    def fail(_planned):
        msg = "the dataset has no state_fips column"
        raise KeyError(msg)

    with pytest.raises(ReportExecutionError) as raised:
        _run(_call(), calculator=fail)

    assert raised.value.failure.error_code


def test_the_contract_names_the_labour_supply_parameters_policyengine_checks() -> None:
    assert LABOR_SUPPLY_RESPONSE_PARAMETER_PREFIXES == {
        "us": py_labor_supply.US_LSR_PARAMETER_PREFIXES,
        "uk": py_labor_supply.UK_LSR_PARAMETER_PREFIXES,
    }
