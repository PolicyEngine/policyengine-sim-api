"""Fixed inputs for the tests that pin what the US precompute produces today.

The precompute, the deploy manifest and the runtime lookup read the release
bundle, the bundle receipt, .py's release manifest, the SPM capability, the US
country model's regions and the artifact store. ``install_us_prebuilt_inputs``
replaces each with a fixed stand-in, so the plan, the manifest and the lookup
outcomes are pure functions of the values below and can be compared with
recorded values exactly:

* the identity seams use ``identity_stubs``' synthetic version-set, with a
  default dataset ``populace_cps`` and a state-level dataset
  ``populace_cps_local``;
* the 51 US state regions (50 states and DC) have the real region codes, state
  codes and FIPS numbers, scoped by a ``state_fips`` row filter and loading the
  state-level dataset, as today's US model's states are;
* the artifact store is a fake GCS client that records every upload.

The national partition is NOT replaced: plans use the real 20 region groups.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from google.api_core.exceptions import PreconditionFailed
from policyengine.core.scoping_strategy import RowFilterStrategy
from policyengine_simulation_contract.spm import SPMCapability
from policyengine_simulation_executor import (
    artifact_store,
    region_resolution,
    release_bundle,
)
from policyengine_simulation_executor import simulation_runtime as runtime

from fixtures.identity_stubs import install_identity_stubs

if TYPE_CHECKING:
    import pytest

# (state code, FIPS number) for the 50 states and DC, in FIPS order.
US_STATE_FIPS: tuple[tuple[str, int], ...] = (
    ("AL", 1),
    ("AK", 2),
    ("AZ", 4),
    ("AR", 5),
    ("CA", 6),
    ("CO", 8),
    ("CT", 9),
    ("DE", 10),
    ("DC", 11),
    ("FL", 12),
    ("GA", 13),
    ("HI", 15),
    ("ID", 16),
    ("IL", 17),
    ("IN", 18),
    ("IA", 19),
    ("KS", 20),
    ("KY", 21),
    ("LA", 22),
    ("ME", 23),
    ("MD", 24),
    ("MA", 25),
    ("MI", 26),
    ("MN", 27),
    ("MS", 28),
    ("MO", 29),
    ("MT", 30),
    ("NE", 31),
    ("NV", 32),
    ("NH", 33),
    ("NJ", 34),
    ("NM", 35),
    ("NY", 36),
    ("NC", 37),
    ("ND", 38),
    ("OH", 39),
    ("OK", 40),
    ("OR", 41),
    ("PA", 42),
    ("RI", 44),
    ("SC", 45),
    ("SD", 46),
    ("TN", 47),
    ("TX", 48),
    ("UT", 49),
    ("VT", 50),
    ("VA", 51),
    ("WA", 53),
    ("WV", 54),
    ("WI", 55),
    ("WY", 56),
)

DEFAULT_DATASET = "populace_cps"
STATE_LEVEL_DATASET = "populace_cps_local"
UK_DEFAULT_DATASET = "enhanced_frs"


@dataclass
class RecordingGcsClient:
    """A fake GCS client: ``objects`` maps paths to bytes, ``uploads`` records writes.

    Each entry of ``uploads`` is ``(bucket, path, data, keyword arguments)``.
    A write with ``if_generation_match=0`` to an existing path leaves the
    object unchanged and raises ``PreconditionFailed``, as GCS does.
    """

    objects: dict[str, bytes] = field(default_factory=dict)
    uploads: list[tuple[str, str, bytes, dict[str, Any]]] = field(default_factory=list)

    def bucket(self, bucket_name: str) -> SimpleNamespace:
        """Return a bucket handle whose blobs read and write ``objects``."""
        return SimpleNamespace(
            blob=lambda path: _RecordingBlob(self, bucket_name, path)
        )


@dataclass
class _RecordingBlob:
    client: RecordingGcsClient
    bucket_name: str
    path: str

    def exists(self) -> bool:
        return self.path in self.client.objects

    def upload_from_string(self, data: str | bytes, **keyword_arguments: Any) -> None:  # noqa: ANN401  # mirrors google.cloud.storage.Blob's keyword arguments
        payload = data.encode() if isinstance(data, str) else data
        self.client.uploads.append(
            (self.bucket_name, self.path, payload, dict(keyword_arguments))
        )
        if (
            keyword_arguments.get("if_generation_match") == 0
            and self.path in self.client.objects
        ):
            msg = f"object exists: {self.path}"
            raise PreconditionFailed(msg)
        self.client.objects[self.path] = payload


def us_state_regions(dataset_uri: str) -> list[SimpleNamespace]:
    """Return stand-ins for the US model's 51 state regions, loading ``dataset_uri``."""
    return [
        SimpleNamespace(
            code=f"state/{state_code.lower()}",
            label=state_code,
            dataset_path=dataset_uri,
            scoping_strategy=RowFilterStrategy(
                variable_name="state_fips", variable_value=state_fips
            ),
            parent_code="us",
            state_code=state_code,
        )
        for state_code, state_fips in US_STATE_FIPS
    ]


def _uk_bundle() -> SimpleNamespace:
    return SimpleNamespace(
        country="uk",
        policyengine_version="4.22.0",
        model_version="2.0.0",
        data_version="3.4.5",
        data_package_version="3.4.5",
        data_artifact_revision="rev-uk",
        default_dataset=UK_DEFAULT_DATASET,
        dataset_uris={
            UK_DEFAULT_DATASET: "hf://org/uk-repo/enhanced_frs.h5@rev-uk",
        },
    )


def install_us_prebuilt_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Replace every input of the US precompute and lookup with a fixed stand-in.

    Returns the identity stub state (``identity``, whose ``spm_capability`` a
    test may set), the US and UK bundles, the 51 state regions, and the fake
    GCS client every ``ArtifactStore`` built without a client uses.
    """
    identity = install_identity_stubs(monkeypatch)
    us_bundle = identity.bundle
    # Region resolution re-pins a region's dataset path with these revisions.
    us_bundle.data_package_version = us_bundle.data_version
    uk_bundle = _uk_bundle()

    def country_bundle(country: str) -> SimpleNamespace:
        return uk_bundle if country.lower() == "uk" else us_bundle

    for module in (release_bundle, runtime, region_resolution):
        monkeypatch.setattr(module, "get_country_release_bundle", country_bundle)

    regions = us_state_regions(us_bundle.dataset_uris[STATE_LEVEL_DATASET])
    regions_by_code = {region.code: region for region in regions}
    us_module = SimpleNamespace(
        model=SimpleNamespace(
            get_region=regions_by_code.get,
            region_registry=SimpleNamespace(
                get_by_type=lambda region_type: (
                    list(regions) if region_type == "state" else []
                )
            ),
        )
    )
    monkeypatch.setattr(runtime, "_country_module", lambda _country: us_module)

    gcs_client = RecordingGcsClient()
    monkeypatch.setattr(artifact_store, "_default_client", lambda: gcs_client)
    return SimpleNamespace(
        identity=identity,
        us_bundle=us_bundle,
        uk_bundle=uk_bundle,
        regions=regions,
        gcs_client=gcs_client,
    )


def fixed_spm_capability() -> SPMCapability:
    """Return an SPM capability whose defaults are fixed values.

    The installed image's capability pins the forecast it ships; this one pins
    a synthetic forecast hash, so SPM-keyed identities stay fixed.
    """
    return SPMCapability.model_validate(
        {
            "defaults": {
                "forecast_content_sha256": "a" * 64,
                "scenario": "ce_trend",
                "geography_kind": "national",
                "geography_id": None,
                "county_vintage": "2020",
                "as_of": None,
            }
        }
    )


def enable_fixed_spm_capability(
    monkeypatch: pytest.MonkeyPatch, inputs: SimpleNamespace
) -> None:
    """Make the US bundle offer ``fixed_spm_capability`` and skip forecast checks.

    ``_prevalidate_selection`` reads the installed forecast to prove a
    selection can be measured; the synthetic hash names no real forecast.
    """
    from policyengine_simulation_executor import spm as executor_spm

    inputs.identity.spm_capability = fixed_spm_capability()
    monkeypatch.setattr(
        executor_spm,
        "_prevalidate_selection",
        lambda _selection_json, _start_year, _window_size: None,
    )
