"""The ``/versions`` answers built from the v2 version manifest.

The old gateway answers ``GET /versions`` and ``GET /versions/{kind}`` from
v1's routing state; these tests pin the same answers, in the same shapes,
built from the v2 version manifest instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from policyengine_simulation_contract.gateway_models import VersionsResponse
from policyengine_simulation_contract.stage12_manifest import (
    V2VersionManifest,
    V2WorkerVersion,
    v2_application_name,
)
from policyengine_simulation_entry.v2_versions import (
    UnknownVersionKindError,
    version_map_from_manifest,
    versions_from_manifest,
)
from stage12_fixtures import worker

# The gateway's real /versions answer for two published releases; the
# gateway project's test_versions_fixture.py keeps it current.
GATEWAY_VERSIONS_ANSWER = json.loads(
    (Path(__file__).parent / "fixtures" / "versions_body.json").read_text()
)
TEST_DIGEST = "c" * 64


def manifest_worker(
    policyengine_version: str,
    *,
    us_version: str,
    uk_version: str,
) -> V2WorkerVersion:
    """Return the fixture worker, rebuilt for these package versions."""
    worker_document = worker().model_dump(mode="json")
    application_name = v2_application_name(policyengine_version)
    worker_document["application_name"] = application_name
    worker_document["validation"]["application_name"] = application_name
    worker_document["bundle"]["policyengine_version"] = policyengine_version
    worker_document["bundle"]["policyengine_requirement"] = (
        f"policyengine=={policyengine_version}"
    )
    for country_bundle in worker_document["bundle"]["countries"]:
        package_version = {"us": us_version, "uk": uk_version}[
            country_bundle["country"]
        ]
        country_bundle["country_package_version"] = package_version
        country_bundle["country_package_requirement"] = (
            f"{country_bundle['country_package_name']}=={package_version}"
        )
    return V2WorkerVersion.model_validate(worker_document)


def version_manifest(
    *workers: V2WorkerVersion,
    default_version: str,
) -> V2VersionManifest:
    """Return a manifest holding ``workers``, keyed by their bundle versions."""
    return V2VersionManifest(
        generation="test-generation",
        default_version=default_version,
        versions={
            worker_version.bundle.policyengine_version: worker_version
            for worker_version in workers
        },
    )


def with_country_bundle(
    worker_version: V2WorkerVersion,
    country: str,
    **fields: object,
) -> V2WorkerVersion:
    """Return ``worker_version`` with ``fields`` replaced in one country's bundle."""
    worker_document = worker_version.model_dump(mode="json")
    for country_bundle in worker_document["bundle"]["countries"]:
        if country_bundle["country"] == country:
            country_bundle.update(fields)
    return V2WorkerVersion.model_validate(worker_document)


TWO_RELEASES = version_manifest(
    manifest_worker("6.2.6", us_version="1.700.0", uk_version="2.125.1"),
    manifest_worker("6.2.7", us_version="1.700.0", uk_version="2.126.8"),
    default_version="6.2.7",
)


def test_policyengine_routes_each_version_to_its_application_and_latest_to_default():
    answer = version_map_from_manifest(TWO_RELEASES, "policyengine")

    assert answer.root == {
        "6.2.6": "policyengine-simulation-v2-py6-2-6",
        "6.2.7": "policyengine-simulation-v2-py6-2-7",
        "latest": "6.2.7",
    }


def test_country_routes_each_model_version_and_latest_to_the_default_bundles_model():
    assert version_map_from_manifest(TWO_RELEASES, "uk").root == {
        "2.125.1": "policyengine-simulation-v2-py6-2-6",
        "2.126.8": "policyengine-simulation-v2-py6-2-7",
        "latest": "2.126.8",
    }
    assert version_map_from_manifest(TWO_RELEASES, "us").root == {
        "1.700.0": "policyengine-simulation-v2-py6-2-7",
        "latest": "1.700.0",
    }


def test_a_model_version_in_several_bundles_routes_to_the_newest_bundle():
    # 6.10.0 is newer than 6.7.5 by version order, though not as text. The
    # old gateway routes a shared model version to the last bundle published,
    # which in staging and production is the newest.
    manifest = version_manifest(
        manifest_worker("6.10.0", us_version="2.2.1", uk_version="2.132.6"),
        manifest_worker("6.7.5", us_version="2.2.1", uk_version="2.130.1"),
        manifest_worker("6.2.1", us_version="2.2.1", uk_version="2.102.3"),
        default_version="6.10.0",
    )

    assert version_map_from_manifest(manifest, "us").root == {
        "2.2.1": "policyengine-simulation-v2-py6-10-0",
        "latest": "2.2.1",
    }


def test_a_version_that_is_not_only_numbers_counts_as_older_than_releases():
    manifest = version_manifest(
        manifest_worker("6.3.0", us_version="2.2.1", uk_version="2.132.6"),
        manifest_worker("6.4.0rc1", us_version="2.2.1", uk_version="2.132.7"),
        default_version="6.3.0",
    )

    assert version_map_from_manifest(manifest, "us").root["2.2.1"] == (
        "policyengine-simulation-v2-py6-3-0"
    )


def test_latest_follows_the_default_version_rather_than_the_newest():
    manifest = version_manifest(
        manifest_worker("6.2.6", us_version="1.700.0", uk_version="2.125.1"),
        manifest_worker("6.2.7", us_version="1.701.0", uk_version="2.126.8"),
        default_version="6.2.6",
    )

    answer = versions_from_manifest(manifest)

    assert answer.policyengine.root["latest"] == "6.2.6"
    assert answer.us.root["latest"] == "1.700.0"
    assert answer.uk.root["latest"] == "2.125.1"


def test_bundles_report_each_countrys_identity():
    release = with_country_bundle(
        manifest_worker("6.2.7", us_version="1.700.0", uk_version="2.126.8"),
        "uk",
        data_package_version="0.1.0",
        data_release_version="uk-release-1",
        data_artifact_revision="uk-release-1",
        default_dataset="frs_2024",
        default_dataset_uri="hf://policyengine/data/frs_2024.h5@uk-release-1",
        datasets=[
            {
                "identity": "frs_2024",
                "uri": "hf://policyengine/data/frs_2024.h5@uk-release-1",
                "artifact_revision": "uk-release-1",
                "sha256": TEST_DIGEST,
                "repo_type": "dataset",
            },
            {
                "identity": "other_dataset",
                "uri": "hf://policyengine/data/other_dataset.h5@uk-release-1",
                "artifact_revision": "uk-release-1",
                "sha256": "d" * 64,
                "repo_type": "dataset",
            },
        ],
    )

    answer = versions_from_manifest(version_manifest(release, default_version="6.2.7"))

    assert answer.bundles["6.2.7"]["uk"].model_dump(mode="json") == {
        "model_version": "2.126.8",
        "policyengine_version": "6.2.7",
        "data_version": "uk-release-1",
        "dataset": "hf://policyengine/data/frs_2024.h5@uk-release-1",
        "dataset_sha256": TEST_DIGEST,
        "spm": None,
    }
    assert set(answer.bundles["6.2.7"]) == {"us", "uk"}


def test_a_default_dataset_without_a_recorded_digest_reports_none():
    release = with_country_bundle(
        manifest_worker("6.2.7", us_version="1.700.0", uk_version="2.126.8"),
        "us",
        default_dataset="dataset_the_bundle_lists_no_digest_for",
    )

    answer = versions_from_manifest(version_manifest(release, default_version="6.2.7"))

    assert answer.bundles["6.2.7"]["us"].dataset_sha256 is None


def test_a_dataset_that_cannot_be_resolved_is_reported_without_one():
    # The old gateway reports such an entry without a dataset rather than
    # failing the whole answer.
    release = with_country_bundle(
        manifest_worker("6.2.7", us_version="1.700.0", uk_version="2.126.8"),
        "us",
        default_dataset_uri="hf://policyengine/missing-path",
    )

    answer = versions_from_manifest(version_manifest(release, default_version="6.2.7"))

    assert answer.bundles["6.2.7"]["us"].dataset is None
    assert answer.bundles["6.2.7"]["us"].model_version == "1.700.0"


def test_the_manifest_records_no_spm_capabilities():
    # Known difference from the old gateway, which reports each bundle's SPM
    # capability from v1's routing state. The v2 manifest does not record one.
    assert versions_from_manifest(TWO_RELEASES).spm_capabilities == {}


@pytest.mark.parametrize("kind", ["US", "Uk", "POLICYENGINE"])
def test_kind_is_matched_without_regard_to_case(kind):
    assert version_map_from_manifest(TWO_RELEASES, kind) == version_map_from_manifest(
        TWO_RELEASES, kind.lower()
    )


@pytest.mark.parametrize("kind", ["ca", "latest", ""])
def test_an_unknown_kind_is_rejected_with_the_old_gateways_detail(kind):
    with pytest.raises(UnknownVersionKindError) as raised:
        version_map_from_manifest(TWO_RELEASES, kind)

    assert raised.value.detail == f"Unknown version kind: {kind}"


def test_each_kind_answer_equals_that_kinds_map_in_the_whole_answer():
    whole_answer = versions_from_manifest(TWO_RELEASES)

    for kind in ("policyengine", "us", "uk"):
        assert version_map_from_manifest(TWO_RELEASES, kind) == getattr(
            whole_answer, kind
        )


def _json_shape(value: object) -> object:
    """Describe a JSON value by its keys and value types, ignoring the values."""
    if isinstance(value, dict):
        return {key: _json_shape(child) for key, child in value.items()}
    return type(value).__name__


def test_the_answer_has_the_shape_of_the_gateways_answer():
    # The same two releases as the gateway fixture: 6.2.6 and 6.2.7, latest
    # 6.2.7, with the same model versions.
    answer = versions_from_manifest(TWO_RELEASES).model_dump(mode="json")
    gateway_answer = GATEWAY_VERSIONS_ANSWER

    assert set(answer) == set(gateway_answer)
    for kind in ("policyengine", "us", "uk"):
        assert set(answer[kind]) == set(gateway_answer[kind])
        assert all(isinstance(value, str) for value in answer[kind].values())
        assert answer[kind]["latest"] == gateway_answer[kind]["latest"]
    assert set(answer["bundles"]) == set(gateway_answer["bundles"])
    for version, countries in gateway_answer["bundles"].items():
        assert set(answer["bundles"][version]) == set(countries)
        for country, identity in countries.items():
            built_identity = answer["bundles"][version][country]
            assert set(built_identity) == set(identity)
            assert built_identity["model_version"] == identity["model_version"]
            assert (
                built_identity["policyengine_version"]
                == identity["policyengine_version"]
            )
            assert _json_shape(built_identity["spm"]) == _json_shape(identity["spm"])
    # Both answers read back as the same response model, unchanged.
    for whole_answer in (answer, gateway_answer):
        assert (
            VersionsResponse.model_validate(whole_answer).model_dump(mode="json")
            == whole_answer
        )
