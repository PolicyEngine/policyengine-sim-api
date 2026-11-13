"""The v2 worker version manifest's models, and the v2 application name.

A manifest (``V2VersionManifest``) lists each deployed v2 worker application
(``V2WorkerVersion``): the countries it serves and what each can calculate
(``V2CountryWorker``, with ``LocalAreaReports`` for the UK), the bundle it
runs, and its validation receipt. ``stage12_manifest`` stores and reads it,
and re-exports every name here.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from policyengine_simulation_contract.stage12_bundle import (
    CountryId,
    Sha256Digest,
    Stage12BundleManifest,
)

V2_APPLICATION_PREFIX = "policyengine-simulation-v2-py"
V1_APPLICATION_PREFIX = "policyengine-simulation-py"
_MAX_MODAL_APPLICATION_NAME_LENGTH = 64
_CALLABLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
# The UK local-area report run input a coordinator runs: ``breakdown:
# "local_areas"`` with the geographic aggregate alone. A later change to that
# input raises it.
LOCAL_AREA_REPORTS_VERSION = 1

ManifestText = Annotated[str, Field(min_length=1, max_length=255)]
CallableName = Annotated[str, Field(pattern=_CALLABLE_PATTERN.pattern)]


class StrictManifestModel(BaseModel):
    """Base for v2 version manifest models; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalAreaReports(StrictManifestModel):
    """The UK local-area report runs a worker application's coordinator runs.

    ``input_version`` is the ``LOCAL_AREA_REPORTS_VERSION`` of the input it
    accepts. ``dataset`` is the dataset a UK-wide or nation report's area part
    is calculated on, which the release routes constituency and
    local-authority regions to; None when the release routes them to no
    dataset of their own, so a national run calculates both area levels
    itself. The worker decides both by the rule its national aggregation
    uses (``uk_area_regions_routed``).
    """

    input_version: Annotated[int, Field(ge=1)]
    dataset: ManifestText | None


class V2CountryWorker(StrictManifestModel):
    """A country served by a v2 worker application, and what it can calculate.

    An economy-capable country names the callable that runs one of its report
    simulations; only such a country's reports are given to the report
    coordinator. ``household_callable`` names the function that calculates
    its households, and is None when the application has none or it was not
    validated. ``local_area_reports`` is set only on an economy-capable UK
    entry whose coordinator runs local-area report runs, and left out of the
    JSON otherwise, so other entries are written, and digested, as before.
    """

    country: CountryId
    economy_capable: bool
    single_simulation_callable: CallableName | None = None
    household_callable: CallableName | None = None
    local_area_reports: LocalAreaReports | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_capabilities(self) -> V2CountryWorker:
        """Require a simulation callable exactly when the country is economy-capable.

        A country must also have at least one capability.
        """
        if self.economy_capable != (self.single_simulation_callable is not None):
            msg = (
                "an economy-capable country, and only one, names a simulation callable"
            )
            raise ValueError(msg)
        if not self.economy_capable and self.household_callable is None:
            msg = "a country needs at least one capability"
            raise ValueError(msg)
        if self.local_area_reports is not None and (
            self.country != "uk" or not self.economy_capable
        ):
            msg = "only an economy-capable UK entry runs local-area report runs"
            raise ValueError(msg)
        return self


class Stage12CacheRelease(StrictManifestModel):
    """The US cache a worker image holds: its manifest digest and coverage.

    The cache covers 3 datasets and 60 baselines, one per region-group segment
    and year.
    """

    manifest_sha256: Sha256Digest
    partition_sha256: Sha256Digest
    years: tuple[int, ...]
    segment_count: Literal[20] = 20
    dataset_count: Literal[3] = 3
    baseline_count: Literal[60] = 60


class V2WorkerValidation(StrictManifestModel):
    """Receipt for a successful validation of a deployed v2 worker application.

    Names the application and bundle digest that were validated, when, and the
    validation invocation IDs: overall, for each economy-capable country's
    simulation worker, and for each country's household function.
    """

    validated: Literal[True]
    application_name: ManifestText
    bundle_manifest_sha256: Sha256Digest
    validated_at: datetime
    validation_invocation_id: ManifestText
    country_validation_invocation_ids: dict[CountryId, ManifestText]
    household_validation_invocation_ids: dict[CountryId, ManifestText] = Field(
        default_factory=dict
    )
    cache: Stage12CacheRelease | None = None


def _require_local_area_datasets(
    countries: tuple[V2CountryWorker, ...], bundle: Stage12BundleManifest
) -> None:
    """Require each declared local-area dataset to be a dataset of the country."""
    for worker in countries:
        declared = worker.local_area_reports
        if declared is None or declared.dataset is None:
            continue
        identities = {
            dataset.identity
            for country in bundle.countries
            if country.country == worker.country
            for dataset in country.datasets
        }
        if declared.dataset not in identities:
            msg = (
                f"the {worker.country} local-area dataset {declared.dataset!r} is "
                "not a dataset of its bundle"
            )
            raise ValueError(msg)


def _require_function_receipts(
    countries: tuple[V2CountryWorker, ...], validation: V2WorkerValidation
) -> None:
    """Require a validation invocation for exactly the functions countries name."""
    economy_countries = {
        worker.country for worker in countries if worker.economy_capable
    }
    if set(validation.country_validation_invocation_ids) != economy_countries:
        msg = "worker validation does not cover exactly the economy-capable countries"
        raise ValueError(msg)
    household_countries = {
        worker.country for worker in countries if worker.household_callable is not None
    }
    if set(validation.household_validation_invocation_ids) != household_countries:
        msg = "worker validation does not cover exactly the household functions"
        raise ValueError(msg)


class V2WorkerVersion(StrictManifestModel):
    """One deployed v2 worker application and the bundle it runs.

    Lists its report coordinator callable, its per-country workers and its
    validation receipt; the countries must match the bundle's, in order, and
    every function a country names must have been validated.
    """

    application_name: ManifestText
    report_coordinator_callable: CallableName
    countries: tuple[V2CountryWorker, ...]
    bundle: Stage12BundleManifest
    bundle_manifest_sha256: Sha256Digest
    validation: V2WorkerValidation
    cache: Stage12CacheRelease | None = None

    @model_validator(mode="after")
    def validate_worker(self) -> V2WorkerVersion:
        """Check the worker's name, validation receipt and countries match its bundle.

        The application name must be the v2 name derived from the bundle's
        PolicyEngine.py version, the validation must name this application and
        bundle digest, and the countries must equal the bundle's, in order,
        each named function with a validation invocation.
        """
        if not self.application_name.startswith(V2_APPLICATION_PREFIX):
            msg = "v2 application uses an invalid name prefix"
            raise ValueError(msg)
        if self.application_name.startswith(V1_APPLICATION_PREFIX):
            msg = "v2 application name collides with the v1 prefix"
            raise ValueError(msg)
        expected_name = v2_application_name(self.bundle.policyengine_version)
        if self.application_name != expected_name:
            msg = "v2 application name does not match its bundle version"
            raise ValueError(msg)
        if self.validation.application_name != self.application_name:
            msg = "worker validation names another application"
            raise ValueError(msg)
        if self.validation.bundle_manifest_sha256 != self.bundle_manifest_sha256:
            msg = "worker validation names another bundle digest"
            raise ValueError(msg)
        if self.validation.cache != self.cache:
            msg = "worker cache metadata differs from worker validation"
            raise ValueError(msg)
        countries = tuple(worker.country for worker in self.countries)
        bundle_countries = tuple(country.country for country in self.bundle.countries)
        if len(countries) != len(set(countries)):
            msg = "v2 worker countries must be unique"
            raise ValueError(msg)
        if countries != bundle_countries:
            msg = "v2 worker countries do not match the normalized bundle"
            raise ValueError(msg)
        _require_function_receipts(self.countries, self.validation)
        _require_local_area_datasets(self.countries, self.bundle)
        return self

    def economy_worker(self, country: str) -> V2CountryWorker | None:
        """Return ``country``'s worker when it can run economy reports."""
        return next(
            (
                worker
                for worker in self.countries
                if worker.country == country and worker.economy_capable
            ),
            None,
        )

    def economy_simulation_callable(self, country: str) -> str:
        """Return the callable that runs one of ``country``'s report simulations.

        Raises LookupError when the manifest does not mark ``country`` as
        economy-capable.
        """
        worker = self.economy_worker(country)
        if worker is None or worker.single_simulation_callable is None:
            msg = (
                f"country {country!r} is not economy-capable in {self.application_name}"
            )
            raise LookupError(msg)
        return worker.single_simulation_callable

    def household_worker(self, country: str) -> V2CountryWorker | None:
        """Return ``country``'s worker when it has a household function."""
        return next(
            (
                worker
                for worker in self.countries
                if worker.country == country and worker.household_callable is not None
            ),
            None,
        )


class V2VersionManifest(StrictManifestModel):
    """Registry of deployed v2 worker versions, keyed by PolicyEngine.py version.

    ``default_version`` is the version used when a request does not pin one, and
    ``generation`` identifies the publish that produced this manifest.
    """

    schema_version: Literal[2] = 2
    generation: ManifestText
    default_version: ManifestText
    versions: dict[str, V2WorkerVersion]

    @model_validator(mode="after")
    def validate_versions(self) -> V2VersionManifest:
        """Check the default has an entry and each key is its bundle's version."""
        if not self.versions:
            msg = "v2 manifest must contain a worker version"
            raise ValueError(msg)
        if self.default_version not in self.versions:
            msg = "v2 default version has no worker entry"
            raise ValueError(msg)
        for version, worker in self.versions.items():
            if version != worker.bundle.policyengine_version:
                msg = "v2 manifest version key does not match its bundle"
                raise ValueError(msg)
        return self


def v2_application_name(policyengine_version: str) -> str:
    """Derive the v2 Modal application name from a PolicyEngine.py version.

    Dots and plus signs become hyphens. Raises ValueError for a version with
    characters a Modal name cannot hold, or a name over 64 characters.
    """
    if not policyengine_version or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._+-]*", policyengine_version
    ):
        msg = "PolicyEngine.py version cannot form a Modal application name"
        raise ValueError(msg)
    name = V2_APPLICATION_PREFIX + policyengine_version.replace(".", "-").replace(
        "+", "-"
    )
    if len(name) > _MAX_MODAL_APPLICATION_NAME_LENGTH:
        msg = (
            "v2 Modal application name exceeds "
            f"{_MAX_MODAL_APPLICATION_NAME_LENGTH} characters"
        )
        raise ValueError(msg)
    return name
