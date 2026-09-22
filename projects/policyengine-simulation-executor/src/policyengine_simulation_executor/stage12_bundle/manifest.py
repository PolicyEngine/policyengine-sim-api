"""Normalization and loading of the packaged PolicyEngine.py bundle."""

from __future__ import annotations

import json
from collections.abc import Sequence
from hashlib import sha256

from policyengine_simulation_contract.stage12_bundle import (
    SUPPORTED_COUNTRIES,
    CountryId,
    ResolvedStage12Bundle,
    Stage12BundleManifest,
)
from pydantic import ValidationError

from .errors import Stage12BundleError
from .parsing import (
    normalize_country_bundle,
    require_exact_package,
    require_mapping,
    require_text,
)


def _normalize_stage12_bundle(
    raw_bundle: object,
    *,
    supported_countries: Sequence[CountryId] = SUPPORTED_COUNTRIES,
) -> Stage12BundleManifest:
    bundle = require_mapping(raw_bundle, "root")
    if bundle.get("schema_version") != 2:
        raise Stage12BundleError("PolicyEngine.py bundle schema_version must be 2")
    policyengine_version = require_text(
        bundle.get("policyengine_version"),
        "policyengine_version",
    )
    if (
        require_text(bundle.get("bundle_version"), "bundle_version")
        != policyengine_version
    ):
        raise Stage12BundleError(
            "PolicyEngine.py bundle_version does not match policyengine_version"
        )

    packages = require_mapping(bundle.get("packages"), "packages")
    packaged_policyengine_version, policyengine_requirement = require_exact_package(
        packages,
        "policyengine",
    )
    if packaged_policyengine_version != policyengine_version:
        raise Stage12BundleError(
            "PolicyEngine.py package version does not match the bundle version"
        )
    core_package_version, core_package_requirement = require_exact_package(
        packages,
        "policyengine-core",
    )
    countries = require_mapping(bundle.get("countries"), "countries")
    data_releases = require_mapping(bundle.get("data_releases"), "data_releases")

    normalized_countries = tuple(
        normalize_country_bundle(
            country=country,
            policyengine_version=policyengine_version,
            countries=countries,
            packages=packages,
            data_releases=data_releases,
        )
        for country in supported_countries
    )
    return Stage12BundleManifest(
        policyengine_version=policyengine_version,
        policyengine_requirement=policyengine_requirement,
        core_package_version=core_package_version,
        core_package_requirement=core_package_requirement,
        countries=normalized_countries,
    )


def normalize_stage12_bundle(
    raw_bundle: object,
    *,
    supported_countries: Sequence[CountryId] = SUPPORTED_COUNTRIES,
) -> Stage12BundleManifest:
    """Normalize only values certified by the packaged `.py` bundle."""

    try:
        return _normalize_stage12_bundle(
            raw_bundle,
            supported_countries=supported_countries,
        )
    except ValidationError as error:
        raise Stage12BundleError(
            "PolicyEngine.py bundle contains malformed values"
        ) from error


def bundle_digest(bundle: Stage12BundleManifest) -> str:
    payload = json.dumps(
        bundle.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return sha256(payload).hexdigest()


def resolve_stage12_bundle(raw_bundle: object) -> ResolvedStage12Bundle:
    bundle = normalize_stage12_bundle(raw_bundle)
    return ResolvedStage12Bundle(
        bundle=bundle,
        bundle_manifest_sha256=bundle_digest(bundle),
    )


def load_stage12_bundle() -> ResolvedStage12Bundle:
    """Read the installed PolicyEngine.py release's packaged bundle."""

    from policyengine.bundle import get_current_bundle

    return resolve_stage12_bundle(get_current_bundle())
