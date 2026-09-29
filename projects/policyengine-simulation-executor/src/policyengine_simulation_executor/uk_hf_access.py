"""Live access validation for every UK dataset in the release bundle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from policyengine_simulation_contract.hf_dataset import (
    HFRepositoryType,
    parse_hf_dataset_uri,
    validate_hf_artifact_uri,
)

from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle


class UKHFDatasetAccessReport(BaseModel):
    """Typed result of validating the complete UK bundle dataset set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    country: Literal["uk"] = "uk"
    dataset_count: int = Field(gt=0)
    dataset_identities: tuple[str, ...]
    repository_ids: tuple[str, ...]
    repository_types: tuple[HFRepositoryType, ...]
    revisions: tuple[str, ...]


DatasetURIValidator = Callable[[str, HFRepositoryType], str]


def validate_uk_hf_dataset_access(
    *,
    environment: Mapping[str, str] | None = None,
    uri_validator: DatasetURIValidator = validate_hf_artifact_uri,
) -> UKHFDatasetAccessReport:
    """Use ``HF_TOKEN`` to validate every UK dataset URI in the bundle.

    This deliberately reads dataset identities, repository paths, and revisions
    from the installed PolicyEngine.py bundle. CI must not maintain a second,
    independently editable list of private UK artifacts.
    """

    runtime_environment = environment if environment is not None else os.environ
    if not runtime_environment.get("HF_TOKEN"):
        raise RuntimeError("HF_TOKEN is required to validate UK dataset access")

    resolved = load_stage12_bundle()
    uk_bundle = next(
        country for country in resolved.bundle.countries if country.country == "uk"
    )
    if not uk_bundle.datasets:
        raise RuntimeError("The PolicyEngine.py bundle declares no UK datasets")

    repository_ids: set[str] = set()
    repository_types: set[HFRepositoryType] = set()
    revisions: set[str] = set()
    identities: list[str] = []
    for dataset in uk_bundle.datasets:
        reference = parse_hf_dataset_uri(dataset.uri)
        if reference is None:
            raise RuntimeError(
                f"UK dataset {dataset.identity!r} is not a Hugging Face artifact"
            )
        if reference.revision is None:
            raise RuntimeError(
                f"UK dataset {dataset.identity!r} does not pin a Hugging Face revision"
            )
        if reference.revision != dataset.artifact_revision:
            raise RuntimeError(
                f"UK dataset {dataset.identity!r} URI revision differs from its "
                "bundle artifact revision"
            )

        if dataset.repo_type == "dataset":
            repository_type: HFRepositoryType = "dataset"
        elif dataset.repo_type == "model":
            repository_type = "model"
        else:
            raise RuntimeError(
                f"UK dataset {dataset.identity!r} declares unsupported Hugging Face "
                f"repository type {dataset.repo_type!r}"
            )

        validated_uri = uri_validator(dataset.uri, repository_type)
        if validated_uri != dataset.uri:
            raise RuntimeError(
                f"UK dataset validator changed the URI for {dataset.identity!r}"
            )
        repository_ids.add(reference.repo_id)
        repository_types.add(repository_type)
        revisions.add(reference.revision)
        identities.append(dataset.identity)

    return UKHFDatasetAccessReport(
        dataset_count=len(identities),
        dataset_identities=tuple(identities),
        repository_ids=tuple(sorted(repository_ids)),
        repository_types=tuple(sorted(repository_types)),
        revisions=tuple(sorted(revisions)),
    )
