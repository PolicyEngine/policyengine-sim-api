"""One-household real formula smoke, enabled explicitly with a local native H5.

This test never loads or simulates the full population. It selects one household
and its native member records using the producer's HDF table indexes.
"""

import json
import os

import pytest

from native_spm_support import materialize_native_household

SOURCE = os.environ.get("SPM_NATIVE_SMOKE_SOURCE")
pytestmark = pytest.mark.skipif(
    not SOURCE,
    reason="Set SPM_NATIVE_SMOKE_SOURCE to the local native H5 for the bounded source integration smoke",
)


@pytest.fixture
def native_dataset(tmp_path):
    return materialize_native_household(os.environ["SPM_NATIVE_SMOKE_SOURCE"], tmp_path)


def test_worker_baseline_reform_national_local_cache_and_receipts(
    native_dataset, tmp_path
):
    from policyengine.core.simulation import _cache
    from policyengine_simulation_executor.simulation_runtime import (
        DatasetSelection,
        _build_simulation,
    )
    from policyengine_simulation_executor.spm import simulation_spm_result

    params = {
        "country": "us",
        "scope": "macro",
        "time_period": "2024",
        "data": str(tmp_path / "native.h5"),
        "spm": {"geography_kind": "national"},
    }
    dataset_selection = DatasetSelection("native", str(tmp_path / "native.h5"), False)
    baseline = _build_simulation(
        params,
        dataset=native_dataset,
        dataset_selection=dataset_selection,
        policy=None,
        region_code="us",
    )
    reform = _build_simulation(
        params,
        dataset=native_dataset,
        dataset_selection=dataset_selection,
        policy={"gov.irs.credits.ctc.amount.base[0].amount": 3000},
        region_code="us",
    )
    for simulation in (baseline, reform):
        simulation.extra_variables = {"spm_unit": ["spm_unit_spm_threshold"]}
        simulation.ensure()
        assert (
            simulation.output_dataset.data.spm_unit["spm_unit_spm_threshold"] > 0
        ).all()
    result = simulation_spm_result(baseline, reform, baseline.spm_config)
    dumped = json.loads(json.dumps(result))
    assert dumped["spm_config"] == baseline.spm_config == reform.spm_config
    assert dumped["spm_provenance"]["reform"][0]["years"]["2024"]
    original = baseline.spm_provenance()
    original["years"].clear()
    assert baseline.spm_provenance()["years"]
    # Disk replay uses the same identifier, selected config, and actual receipt.
    _cache._cache.clear()
    replay = _build_simulation(
        params,
        dataset=native_dataset,
        dataset_selection=dataset_selection,
        policy=None,
        region_code="us",
    )
    replay.id = baseline.id
    replay.ensure()
    assert replay.spm_provenance() == baseline.spm_provenance()
    # A shared caller id cannot reuse national output for county selection.
    local_params = {**params, "spm": {"geography_kind": "county"}}
    local = _build_simulation(
        local_params,
        dataset=native_dataset,
        dataset_selection=dataset_selection,
        policy=None,
        region_code="us",
    )
    local.id = baseline.id
    assert local.storage_id != baseline.storage_id
    local.ensure()
    assert local.spm_provenance()["geography_kind"] == "county"

    # The precompute guard compares the planner's storage id against this
    # wrapper property and refuses to publish on a mismatch, so agreement is
    # what makes any canonical artifact publishable. Hermetic CI can only
    # check our side against the wrapper's expression transcribed into
    # test_artifact_keys; this is the same claim against the real wrapper.
    from policyengine_simulation_executor.artifact_keys import canonical_digest
    from policyengine_simulation_executor.spm import normalize_runtime_spm

    for simulation, request in ((baseline, params), (local, local_params)):
        resolved = normalize_runtime_spm(request)
        assert simulation.spm_config == resolved
        assert simulation.storage_id == (
            f"{simulation.id}-spm-{canonical_digest(resolved)}"
        )


def test_native_state_only_worker_requires_geography_and_explicit_national_works(
    native_dataset,
):
    from policyengine_simulation_executor.simulation_runtime import (
        DatasetSelection,
        _build_simulation,
    )
    from policyengine_simulation_contract.spm import spm_error_detail

    native_dataset.data.household.drop(columns=["county_fips"], inplace=True)
    params = {
        "country": "us",
        "scope": "macro",
        "time_period": "2024",
        "data": "test-only-custom-data",
    }
    dataset_selection = DatasetSelection("native", "test-only-custom-data", False)
    simulation = _build_simulation(
        params,
        dataset=native_dataset,
        dataset_selection=dataset_selection,
        policy=None,
        region_code="us",
    )
    with pytest.raises(ValueError) as error:
        simulation.run()
    assert spm_error_detail(error.value).code == "SPM_GEOGRAPHY_REQUIRED"
    national = _build_simulation(
        {**params, "spm": {"geography_kind": "national"}},
        dataset=native_dataset,
        dataset_selection=dataset_selection,
        policy=None,
        region_code="us",
    )
    national.run()
    assert national.spm_provenance()["geography_kind"] == "national"


def test_actual_provider_unknown_metro_is_a_typed_input_error():
    from policyengine_simulation_executor.spm import normalize_runtime_spm
    from policyengine_simulation_contract.spm import SPMInputError

    with pytest.raises(SPMInputError) as error:
        normalize_runtime_spm(
            {
                "country": "us",
                "time_period": "2024",
                "spm": {"geography_kind": "metro", "geography_id": "00000"},
            }
        )
    assert error.value.code == "SPM_GEOGRAPHY_UNAVAILABLE"
