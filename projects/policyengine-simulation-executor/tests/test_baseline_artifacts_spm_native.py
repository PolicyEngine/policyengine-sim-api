"""Real one-household SPM artifacts with deliberate local corruption.

The producer always calculates authentic receipts with the installed country and
calculator. Corruption cases then damage only that temporary result artifact.
"""

import json
import os
from pathlib import Path

import pytest

from native_spm_support import materialize_native_household

pytestmark = pytest.mark.skipif(
    not os.environ.get("SPM_NATIVE_SMOKE_SOURCE"),
    reason="Requires the explicitly selected local native H5 and installed SPM bundle",
)


@pytest.fixture
def native_dataset(tmp_path):
    return materialize_native_household(os.environ["SPM_NATIVE_SMOKE_SOURCE"], tmp_path)


def _baseline(dataset, *, geography="national", tax_only=False):
    from policyengine_simulation_executor.baseline_artifacts import (
        ArtifactBaselineSimulation,
    )
    from policyengine_simulation_executor.simulation_runtime import (
        DatasetSelection,
        _build_simulation,
    )

    built = _build_simulation(
        {
            "country": "us",
            "scope": "macro",
            "time_period": "2024",
            "data": "bounded-native-test",
            "spm": {"geography_kind": geography},
        },
        dataset=dataset,
        dataset_selection=DatasetSelection("bounded-native-test", "native", False),
        policy=None,
        region_code="us",
    )
    baseline = ArtifactBaselineSimulation.model_construct(**built.__dict__)
    baseline.id = "bl1-native-receipt"
    if tax_only:
        # Produce a genuine lazy, tax-only output under the same selection.
        model = baseline.tax_benefit_model_version
        baseline.tax_benefit_model_version = model.model_copy(
            update={
                "entity_variables": {
                    entity: ["income_tax"] if entity == "tax_unit" else []
                    for entity in model.entity_variables
                }
            }
        )
    return baseline


@pytest.mark.parametrize(
    "damage, expected_outcome",
    [
        ("tax_only_columns", "incomplete"),
        ("empty_years", "incomplete"),
        ("missing_receipt", "miss"),
        ("malformed_receipt", "miss"),
        ("different_selection", "miss"),
        ("corrupt_hdf", "miss"),
    ],
)
def test_incomplete_or_corrupt_spm_disk_artifact_recomputes(
    native_dataset, monkeypatch, damage, expected_outcome
):
    import h5py
    from policyengine.core.simulation import _cache
    from policyengine_simulation_executor.baseline_artifacts import (
        ArtifactBaselineSimulation,
    )
    from policyengine_simulation_executor.spm import simulation_spm_result

    _cache._cache.clear()
    producer = _baseline(native_dataset, tax_only=damage == "tax_only_columns")
    producer.run()
    producer.save()
    path = Path(producer.output_dataset.filepath)
    if damage == "tax_only_columns":
        assert producer.spm_provenance()["years"] == {}
        assert (
            "spm_unit_is_in_spm_poverty"
            not in producer.output_dataset.data.spm_unit.columns
        )
    elif damage == "corrupt_hdf":
        path.write_bytes(b"deliberately corrupted test artifact")
    else:
        with h5py.File(path, "a") as stream:
            recorded = json.loads(stream["policyengine_spm"].asstr()[()])
            del stream["policyengine_spm"]
            if damage == "empty_years":
                recorded["provenance"]["years"] = {}
            elif damage == "malformed_receipt":
                recorded["provenance"]["unexpected_test_field"] = True
            elif damage == "different_selection":
                recorded["config"]["geography_kind"] = "county"
            if damage != "missing_receipt":
                stream.create_dataset(
                    "policyengine_spm",
                    data=json.dumps(recorded),
                    dtype=h5py.string_dtype("utf-8"),
                )

    runs = []
    real_run = ArtifactBaselineSimulation.run

    def counted_run(simulation):
        runs.append(simulation)
        return real_run(simulation)

    monkeypatch.setattr(ArtifactBaselineSimulation, "run", counted_run)
    consumer = _baseline(native_dataset)
    requested = consumer.spm_config
    consumer.ensure()
    assert consumer.artifact_outcome == expected_outcome
    assert runs == [consumer]
    assert consumer.spm_config == requested
    assert not consumer._missing_output_columns()
    simulation_spm_result(consumer, consumer, requested, expected_year=2024)
    # A second request uses the repaired cache; clearing it tests repaired disk.
    cached = _baseline(native_dataset)
    cached.ensure()
    assert cached.artifact_outcome == "hit"
    _cache._cache.clear()
    reloaded = _baseline(native_dataset)
    reloaded.ensure()
    assert reloaded.artifact_outcome == "hit"
    assert runs == [consumer]
    assert reloaded.spm_provenance() == consumer.spm_provenance()


@pytest.mark.parametrize("damage", ["missing_receipt", "different_selection"])
def test_invalid_spm_cache_entry_recomputes_requested_selection(native_dataset, damage):
    from policyengine.core.simulation import _cache
    from policyengine_simulation_executor.spm import simulation_spm_result

    _cache._cache.clear()
    producer = _baseline(
        native_dataset,
        geography="county" if damage == "different_selection" else "national",
    )
    producer.run()
    if damage == "missing_receipt":
        producer.spm_receipt = None
    consumer = _baseline(native_dataset)
    requested = consumer.spm_config
    # Deliberate local cache corruption must never replace the request selection.
    _cache.add(consumer.storage_id, producer)
    consumer.ensure()
    assert consumer.artifact_outcome == "incomplete"
    assert consumer.spm_config == requested
    simulation_spm_result(consumer, consumer, requested, expected_year=2024)
    later = _baseline(native_dataset)
    later.ensure()
    assert later.artifact_outcome == "hit"
    assert later.spm_provenance() == consumer.spm_provenance()
