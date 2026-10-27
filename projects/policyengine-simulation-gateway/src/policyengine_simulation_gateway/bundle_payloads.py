"""Describe the release bundle a route serves, as responses and jobs report it."""

import re
from typing import Any

from policyengine_simulation_contract.dataset_uri import runtime_dataset_uri
from policyengine_simulation_contract.gateway_models import PolicyEngineBundle
from policyengine_simulation_contract.hf_dataset import HuggingFaceDatasetReferenceError
from policyengine_simulation_contract.spm import SPMCapability, SPMInputError

from policyengine_simulation_gateway.route_resolution import (
    RouteResolution,
    routing_state_bundles,
)
from policyengine_simulation_gateway.routes_common import logger


def _country_bundle_data_version(country_bundle: dict) -> str | None:
    data_version = country_bundle.get("data_version")
    return data_version if isinstance(data_version, str) else None


def _country_bundle_data_package_version(country_bundle: dict) -> str | None:
    package_version = country_bundle.get("data_package_version")
    if isinstance(package_version, str):
        return package_version
    return _country_bundle_data_version(country_bundle)


def _country_bundle_data_artifact_revision(country_bundle: dict) -> str | None:
    artifact_revision = country_bundle.get("data_artifact_revision")
    return artifact_revision if isinstance(artifact_revision, str) else None


def _revision_from_dataset_uri(dataset_uri: str | None) -> str | None:
    if not isinstance(dataset_uri, str) or "@" not in dataset_uri:
        return None
    return dataset_uri.rsplit("@", maxsplit=1)[1]


def _bundle_response_data_version(
    *,
    country_bundle: dict,
    resolved_dataset: str | None,
) -> str | None:
    return _country_bundle_data_version(country_bundle) or _revision_from_dataset_uri(
        resolved_dataset
    )


def _resolve_dataset_uri_from_app_bundle(
    *,
    app_bundle: dict,
    country: str,
) -> str | None:
    country_bundle = app_bundle.get(country.lower())
    if not isinstance(country_bundle, dict):
        return None
    dataset_uri = country_bundle.get("default_dataset_uri")
    if not isinstance(dataset_uri, str):
        return None
    return runtime_dataset_uri(
        dataset_uri,
        default_revision=_country_bundle_data_package_version(country_bundle),
        artifact_revision=_country_bundle_data_artifact_revision(country_bundle),
        validate_hf=False,
    )


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def _certified_dataset_sha256(country_bundle: dict) -> str | None:
    """Return the certified SHA-256 of the app's default dataset, if recorded.

    ``dataset`` in a bundle response is always the app's default dataset URI
    (``_resolve_dataset_uri_from_app_bundle``), so the digest describes it.
    Malformed or missing values are omitted rather than repeated.
    """
    digest = country_bundle.get("default_dataset_sha256")
    # fullmatch: ``$`` would also accept a trailing newline.
    if isinstance(digest, str) and _SHA256_HEX.fullmatch(digest):
        return digest
    return None


def build_policyengine_bundle(
    country: str,
    resolution: RouteResolution,
) -> PolicyEngineBundle:
    """Describe the release bundle ``resolution`` serves for ``country``."""
    app_bundle = resolution.bundle_manifest
    country_bundle = app_bundle.get(country.lower())
    if not isinstance(country_bundle, dict):
        country_bundle = {}
    resolved_dataset = _resolve_dataset_uri_from_app_bundle(
        app_bundle=app_bundle,
        country=country,
    )
    data_version = _bundle_response_data_version(
        country_bundle=country_bundle,
        resolved_dataset=resolved_dataset,
    )
    model_version = country_bundle.get("model_version") or resolution.response_version
    policyengine_version = app_bundle.get(
        "policyengine_version", resolution.policyengine_version
    )
    capability = app_bundle.get("spm") if country.lower() == "us" else None
    if capability is not None:
        try:
            capability = SPMCapability.model_validate(capability)
        except ValueError as exc:
            error_code = "SPM_CONFIGURATION_UNAVAILABLE"
            raise SPMInputError(
                error_code,
                "This worker bundle has invalid canonical SPM capability metadata",
            ) from exc
    return PolicyEngineBundle(
        model_version=str(model_version),
        policyengine_version=(
            str(policyengine_version) if isinstance(policyengine_version, str) else None
        ),
        data_version=str(data_version) if isinstance(data_version, str) else None,
        dataset=resolved_dataset,
        dataset_sha256=_certified_dataset_sha256(country_bundle),
        spm=capability,
    )


def bundle_identities(state: dict) -> dict[str, dict[str, PolicyEngineBundle]]:
    """Each deployed wrapper version's country bundle identity, for ``/versions``.

    Entries without a stated model version are skipped: ``/versions`` reports
    only identities the registry recorded, never a fallback. An entry whose
    dataset cannot be resolved is reported without a dataset rather than
    failing the whole response.
    """
    identities: dict[str, dict[str, PolicyEngineBundle]] = {}
    for policyengine_version, app_bundle in routing_state_bundles(state).items():
        if not isinstance(app_bundle, dict):
            continue
        countries: dict[str, PolicyEngineBundle] = {}
        for country in ("us", "uk"):
            country_bundle = app_bundle.get(country)
            if not isinstance(country_bundle, dict):
                continue
            model_version = country_bundle.get("model_version")
            if not isinstance(model_version, str) or not model_version.strip():
                continue
            try:
                resolved_dataset = _resolve_dataset_uri_from_app_bundle(
                    app_bundle=app_bundle, country=country
                )
            except (ValueError, HuggingFaceDatasetReferenceError) as exc:
                # One historical entry with an unresolvable dataset must not
                # take /versions down for every caller; report it without one.
                logger.warning(
                    "Cannot resolve the %s dataset of policyengine %s "
                    "for /versions: %s",
                    country,
                    policyengine_version,
                    exc,
                )
                resolved_dataset = None
            data_version = _bundle_response_data_version(
                country_bundle=country_bundle, resolved_dataset=resolved_dataset
            )
            wrapper_version = app_bundle.get(
                "policyengine_version", policyengine_version
            )
            countries[country] = PolicyEngineBundle(
                model_version=model_version,
                policyengine_version=(
                    str(wrapper_version) if isinstance(wrapper_version, str) else None
                ),
                data_version=str(data_version)
                if isinstance(data_version, str)
                else None,
                dataset=resolved_dataset,
                dataset_sha256=_certified_dataset_sha256(country_bundle),
            )
        if countries:
            identities[str(policyengine_version)] = countries
    return identities


def bundle_payload(
    bundle: PolicyEngineBundle,
    **dump_kwargs: Any,  # noqa: ANN401  # forwarded unchanged to BaseModel.model_dump
) -> dict:
    """Dump a bundle, omitting fields added after the legacy body when unset.

    The 202 and 500 job bodies splat this dict in directly, bypassing the
    routes' ``response_model_exclude_none``. Every other optional bundle
    field was already emitted as an explicit null before canonical SPM, so
    only the newer keys (``spm``, ``dataset_sha256``) are dropped when they
    are unset: a legacy body stays byte-identical.
    """
    exclude = {key for key in ("spm", "dataset_sha256") if getattr(bundle, key) is None}
    return bundle.model_dump(exclude=exclude or None, **dump_kwargs)
