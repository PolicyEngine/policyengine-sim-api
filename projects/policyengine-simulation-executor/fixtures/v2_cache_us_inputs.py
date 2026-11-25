"""Fixed inputs for the tests that pin what the v2 report app's US cache does today.

The v2 cache (``stage12_cache``) plans, keys and looks up its files from the
installed Stage 12 bundle, the release bundle, .py's dataset names, the SPM
capability, the US country model's regions and its own cache bucket.
``install_v2_cache_us_inputs`` replaces each with a fixed stand-in, so the
plan, the keys, the manifest and the lookup outcomes are pure functions of
the values below and can be compared with recorded values exactly:

* the Stage 12 bundle lists two US datasets, the default ``populace_us_2024``
  and the state-level ``populace_us_2024_acs_local``, with fixed hashes,
  revisions and package versions, and a fixed bundle digest;
* the release bundle maps the same two names to fixed URIs, so state regions
  resolve to the state-level dataset as today's US model's states do;
* the 51 US state regions (50 states and DC) have the real region codes,
  state codes and FIPS numbers, scoped by a ``state_fips`` row filter;
* .py's dataset-name lookups return the dataset's own name;
* the cache bucket is a fake GCS client kept in memory.

The national partition is NOT replaced: plans use the real 20 region groups
in ``stage12_runtime.partition.US_REGION_GROUPS``. The output plan and the
region groups' scope keys come from this repository's code and the installed
.py, as they do in production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from google.api_core.exceptions import NotFound, PreconditionFailed
from policyengine.core.scoping_strategy import RowFilterStrategy
from policyengine_simulation_contract.spm import SPMCapability
from policyengine_simulation_contract.stage12_bundle import (
    ResolvedStage12Bundle,
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
)
from policyengine_simulation_executor import (
    region_resolution,
    release_bundle,
    simulation_runtime,
)
from policyengine_simulation_executor.stage12_cache import (
    precompute_baselines,
    precompute_datasets,
    precompute_planning,
    store,
)
from policyengine_simulation_executor.stage12_runtime import (
    simulation as stage12_simulation,
)

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

DEFAULT_DATASET = "populace_us_2024"
STATE_LEVEL_DATASET = "populace_us_2024_acs_local"
DEFAULT_DATASET_URI = "hf://policyengine/populace-us/populace_us_2024.h5@rev-default"
STATE_LEVEL_DATASET_URI = (
    "hf://policyengine/populace-us/populace_us_2024_acs_local.h5@rev-local"
)
BUNDLE_MANIFEST_SHA256 = "b" * 64
DEFAULT_DATASET_SHA256 = "1" * 64
STATE_LEVEL_DATASET_SHA256 = "2" * 64
POLICYENGINE_VERSION = "6.20.0"
CORE_PACKAGE_VERSION = "3.33.3"
US_PACKAGE_VERSION = "2.2.2"
DATA_PACKAGE_VERSION = "1.4.0"
DATA_ARTIFACT_REVISION = "rev-default"


def fixed_stage12_bundle() -> ResolvedStage12Bundle:
    """Return the installed Stage 12 bundle the pins use, with fixed values."""
    default = Stage12Dataset(
        identity=DEFAULT_DATASET,
        uri=DEFAULT_DATASET_URI,
        artifact_revision=DATA_ARTIFACT_REVISION,
        sha256=DEFAULT_DATASET_SHA256,
        repo_type="dataset",
    )
    state_level = Stage12Dataset(
        identity=STATE_LEVEL_DATASET,
        uri=STATE_LEVEL_DATASET_URI,
        artifact_revision="rev-local",
        sha256=STATE_LEVEL_DATASET_SHA256,
        repo_type="dataset",
    )
    country = Stage12CountryBundle(
        country="us",
        country_package_name="policyengine-us",
        country_package_version=US_PACKAGE_VERSION,
        country_package_requirement=f"policyengine-us=={US_PACKAGE_VERSION}",
        data_package_name="populace-us",
        data_package_version=DATA_PACKAGE_VERSION,
        data_release_version=DATA_PACKAGE_VERSION,
        data_artifact_revision=DATA_ARTIFACT_REVISION,
        default_dataset=DEFAULT_DATASET,
        default_dataset_uri=DEFAULT_DATASET_URI,
        datasets=(default, state_level),
    )
    return ResolvedStage12Bundle(
        bundle=Stage12BundleManifest(
            policyengine_version=POLICYENGINE_VERSION,
            policyengine_requirement=f"policyengine=={POLICYENGINE_VERSION}",
            core_package_version=CORE_PACKAGE_VERSION,
            core_package_requirement=f"policyengine-core=={CORE_PACKAGE_VERSION}",
            countries=(country,),
        ),
        bundle_manifest_sha256=BUNDLE_MANIFEST_SHA256,
    )


def fixed_release_bundle() -> SimpleNamespace:
    """Return the release bundle region and dataset resolution read."""
    return SimpleNamespace(
        country="us",
        policyengine_version=POLICYENGINE_VERSION,
        model_version=US_PACKAGE_VERSION,
        data_version=DATA_PACKAGE_VERSION,
        data_package_version=DATA_PACKAGE_VERSION,
        data_artifact_revision=DATA_ARTIFACT_REVISION,
        default_dataset=DEFAULT_DATASET,
        default_dataset_uri=DEFAULT_DATASET_URI,
        dataset_uris={
            DEFAULT_DATASET: DEFAULT_DATASET_URI,
            STATE_LEVEL_DATASET: STATE_LEVEL_DATASET_URI,
        },
    )


def us_state_regions() -> list[SimpleNamespace]:
    """Return stand-ins for the US model's 51 states, on the state-level dataset."""
    return [
        SimpleNamespace(
            code=f"state/{state_code.lower()}",
            label=state_code,
            dataset_path=STATE_LEVEL_DATASET_URI,
            scoping_strategy=RowFilterStrategy(
                variable_name="state_fips", variable_value=state_fips
            ),
            parent_code="us",
            state_code=state_code,
        )
        for state_code, state_fips in US_STATE_FIPS
    ]


def us_district_region(state_code: str, district_number: int) -> SimpleNamespace:
    """Return a stand-in congressional district of ``state_code``."""
    state_fips = dict(US_STATE_FIPS)[state_code]
    return SimpleNamespace(
        code=f"congressional_district/{state_code}{district_number:02d}",
        label=f"{state_code}-{district_number:02d}",
        dataset_path=STATE_LEVEL_DATASET_URI,
        scoping_strategy=RowFilterStrategy(
            variable_name="congressional_district_geoid",
            variable_value=state_fips * 100 + district_number,
        ),
        parent_code=f"state/{state_code.lower()}",
        state_code=state_code,
    )


def fixed_spm_capability() -> SPMCapability:
    """Return an SPM capability whose defaults are fixed values.

    The installed image's capability pins the forecast it ships; this one
    pins a synthetic forecast hash, so SPM-keyed identities stay fixed.
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


@dataclass
class InMemoryCacheBucket:
    """A fake GCS client over one in-memory bucket.

    ``objects`` maps an object path to its bytes and ``metadata`` to the
    metadata stored with it. Writes with ``if_generation_match=0`` to an
    existing path raise ``PreconditionFailed``, as GCS does.
    """

    objects: dict[str, bytes] = field(default_factory=dict)
    metadata: dict[str, dict[str, str]] = field(default_factory=dict)

    def bucket(self, _bucket_name: str) -> SimpleNamespace:
        """Return a bucket handle whose blobs read and write ``objects``."""
        return SimpleNamespace(blob=lambda path: _InMemoryBlob(self, path))

    def store(self, path: str, payload: bytes) -> None:
        """Hold ``payload`` at ``path`` with the digest and size an upload records."""
        self.objects[path] = payload
        self.metadata[path] = {
            "sha256": sha256(payload).hexdigest(),
            "size_bytes": str(len(payload)),
        }


@dataclass
class _InMemoryBlob:
    bucket_client: InMemoryCacheBucket
    path: str
    metadata: dict[str, str] | None = None

    def exists(self) -> bool:
        return self.path in self.bucket_client.objects

    def reload(self) -> None:
        if self.path not in self.bucket_client.objects:
            raise NotFound(self.path)
        self.metadata = self.bucket_client.metadata.get(self.path)

    def _write(self, payload: bytes, if_generation_match: int | None) -> None:
        if if_generation_match == 0 and self.path in self.bucket_client.objects:
            raise PreconditionFailed(self.path)
        self.bucket_client.objects[self.path] = payload
        self.bucket_client.metadata[self.path] = dict(self.metadata or {})

    def upload_from_string(
        self,
        payload: str | bytes,
        *,
        content_type: str,  # noqa: ARG002  # mirrors google.cloud.storage.Blob
        if_generation_match: int | None = None,
    ) -> None:
        self._write(
            payload.encode() if isinstance(payload, str) else payload,
            if_generation_match,
        )

    def upload_from_filename(
        self, filename: str, *, if_generation_match: int | None = None
    ) -> None:
        self._write(Path(filename).read_bytes(), if_generation_match)

    def download_as_bytes(self) -> bytes:
        if self.path not in self.bucket_client.objects:
            raise NotFound(self.path)
        return self.bucket_client.objects[self.path]

    def download_to_filename(self, filename: str) -> None:
        Path(filename).write_bytes(self.download_as_bytes())


def install_v2_cache_us_inputs(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace every input of the v2 cache's US plan and lookup with a fixed stand-in.

    Returns the Stage 12 and release bundles, the 51 state regions, the
    stand-in US country module, the fake cache bucket and ``spm``, whose
    ``spm_capability`` a test may set (None: no SPM capability).
    """
    # These two modules import .py's manifest helpers by value while loading;
    # import them before the helpers are replaced, so no production module
    # binds a stand-in. .py and the SPM module load only when inputs are
    # installed, as the module-level import ban requires.
    import policyengine.tax_benefit_models.common.model_version
    import policyengine.tax_benefit_models.us.datasets  # noqa: F401  # must load before the manifest stand-ins (see above)
    from policyengine.provenance import manifest as manifest_module
    from policyengine_simulation_executor import spm as executor_spm

    stage12_bundle = fixed_stage12_bundle()
    for module in (
        precompute_planning,
        precompute_datasets,
        precompute_baselines,
        stage12_simulation,
    ):
        monkeypatch.setattr(module, "load_stage12_bundle", lambda: stage12_bundle)

    release = fixed_release_bundle()
    for module in (release_bundle, simulation_runtime, region_resolution):
        monkeypatch.setattr(
            module, "get_country_release_bundle", lambda _country: release
        )

    monkeypatch.setattr(
        manifest_module,
        "resolve_dataset_reference",
        lambda _country, dataset: f"hf://policyengine/populace-us/{dataset}.h5",
    )
    monkeypatch.setattr(
        manifest_module,
        "dataset_logical_name",
        lambda reference: reference.rsplit("/", 1)[-1].split(".h5", 1)[0],
    )

    regions = us_state_regions()
    districts = [us_district_region("UT", 1), us_district_region("CA", 12)]
    regions_by_code = {region.code: region for region in (*regions, *districts)}
    country_module = SimpleNamespace(
        model=SimpleNamespace(
            get_region=regions_by_code.get,
            region_registry=SimpleNamespace(
                get_by_type=lambda region_type: {
                    "state": list(regions),
                    "congressional_district": list(districts),
                }.get(region_type, [])
            ),
        )
    )
    for module in (precompute_planning, precompute_datasets, simulation_runtime):
        monkeypatch.setattr(module, "_country_module", lambda _country: country_module)

    spm_state = SimpleNamespace(spm_capability=None)
    monkeypatch.setattr(
        executor_spm, "runtime_spm_capability", lambda: spm_state.spm_capability
    )
    # Proving a selection can be measured reads the installed forecast; the
    # synthetic forecast hash names none.
    monkeypatch.setattr(
        executor_spm,
        "_prevalidate_selection",
        lambda _selection_json, _start_year, _window_size: None,
    )

    cache_bucket = InMemoryCacheBucket()
    monkeypatch.setattr(store, "_default_client", lambda: cache_bucket)
    return SimpleNamespace(
        stage12_bundle=stage12_bundle,
        release_bundle=release,
        regions=regions,
        districts=districts,
        country_module=country_module,
        cache_bucket=cache_bucket,
        spm=spm_state,
    )


def enable_fixed_spm(inputs: SimpleNamespace) -> dict[str, Any]:
    """Give the US bundle ``fixed_spm_capability`` and return its default selection."""
    capability = fixed_spm_capability()
    inputs.spm.spm_capability = capability
    return capability.defaults.model_dump(mode="json")
