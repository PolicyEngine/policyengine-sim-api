"""Resolve a simulation request's region, and check a local-area breakdown request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.dataset_uri import runtime_dataset_uri
from policyengine_simulation_contract.local_area_breakdown import (
    LOCAL_AREAS_BREAKDOWN,
    local_area_breakdown_error,
)

from policyengine_simulation_executor.release_bundle import (
    get_country_release_bundle,
    resolve_bundle_dataset_name,
)

if TYPE_CHECKING:
    from types import ModuleType

    from policyengine.core import Region

# A bare two-letter US region is a state code, such as "ca".
_US_STATE_CODE_LENGTH = 2


@dataclass(frozen=True)
class RegionResolution:
    """A request's normalized region code, the dataset to load, and how to scope it.

    ``scoping_strategy`` narrows the simulation to the region; None simulates
    the whole dataset, as a national run does. ``state_code`` is the two-letter
    code of the US state a state or congressional-district region lies in, and
    None for every other region.
    """

    code: str
    dataset_reference: str | None = None
    scoping_strategy: Any | None = None
    state_code: str | None = None


def resolve_dataset_reference(country: str) -> str:
    """Return the bundle's default dataset name for ``country``."""
    return resolve_bundle_dataset_name(country, None)


def _normalise_region_code(country: str, region: object) -> str:
    if region is None or str(region).strip() == "":
        return country

    raw = str(region).strip()
    if raw.lower() in {"us", "uk"}:
        return raw.lower()

    if "/" not in raw:
        if country == "us" and len(raw) == _US_STATE_CODE_LENGTH:
            return f"state/{raw.lower()}"
        if country == "uk":
            return f"country/{raw.lower().replace(' ', '_')}"
        return raw

    prefix, value = raw.split("/", maxsplit=1)
    prefix = prefix.lower()
    value = value.strip()
    if prefix == "state":
        value = value.lower()
    elif prefix == "country":
        value = value.lower().replace(" ", "_")
    elif prefix in {"congressional_district", "place", "local_authority"}:
        value = value.upper()
    return f"{prefix}/{value}"


def _uk_area_regions_routed() -> bool:
    from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # the UK metadata module is not loaded at import; loaded per call
        uk_area_regions_routed,
    )

    return uk_area_regions_routed()


def _registry_uk_area_region_code(country_module: ModuleType, region_code: str) -> str:
    """Spell a UK constituency or local authority as the region registry keys it.

    When the bundle routes UK area regions, policyengine.py's registry holds
    every constituency and local authority under its GSS code, labelled with
    its name. A request may name the area instead, as policyengine-api's
    ``constituency/{name}`` does, or write the code in lower case; both map to
    the registry's code. Any other value is returned unchanged, so an area
    outside the registry, such as one of the 17 districts replaced in April
    2023, stays unsupported.
    """
    from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # the UK metadata module is not loaded at import; loaded per call
        UK_AREA_REGION_TYPES,
    )

    region_type, _, value = region_code.partition("/")
    if region_type not in UK_AREA_REGION_TYPES or not _uk_area_regions_routed():
        return region_code
    requested = value.casefold()
    for region in country_module.model.region_registry.get_by_type(region_type):
        code = region.code.partition("/")[2]
        if requested in {code.casefold(), region.label.casefold()}:
            return region.code
    return region_code


def _region_parent_dataset_reference(
    country_module: ModuleType,
    country: str,
    region: Region,
) -> str:
    parent_code = getattr(region, "parent_code", None)
    visited: set[str] = set()

    while isinstance(parent_code, str) and parent_code:
        if parent_code in visited:
            msg = f"Region hierarchy cycle at {parent_code}"
            raise ValueError(msg)
        visited.add(parent_code)

        parent_region = country_module.model.get_region(parent_code)
        parent_dataset_path = getattr(parent_region, "dataset_path", None)
        if isinstance(parent_dataset_path, str):
            bundle = get_country_release_bundle(country)
            return runtime_dataset_uri(
                parent_dataset_path,
                default_revision=bundle.data_package_version,
                artifact_revision=bundle.data_artifact_revision,
                validate_hf=False,
            )
        parent_code = getattr(parent_region, "parent_code", None)

    return resolve_dataset_reference(country)


def _reject_unscoped_us_place_region(region_code: str, region: Region) -> None:
    if not region_code.startswith("place/"):
        return
    if getattr(region, "dataset_path", None) is not None:
        return
    if getattr(region, "scoping_strategy", None) is not None:
        return
    msg = (
        "US place regions are not yet supported for runtime simulation because "
        "policyengine.py does not expose place-level dataset scoping."
    )
    raise ValueError(msg)


def resolve_region(
    *,
    country_module: ModuleType,
    country: str,
    params: dict[str, Any],
) -> RegionResolution:
    """Resolve the request's region to its code, scoping and dataset."""
    if params.get("region_group"):
        return _resolve_region_group(
            country_module=country_module, country=country, params=params
        )
    region_code = _normalise_region_code(country, params.get("region"))
    if region_code == country:
        return RegionResolution(
            code=region_code,
            dataset_reference=resolve_dataset_reference(country),
        )

    if country == "uk":
        region_code = _registry_uk_area_region_code(country_module, region_code)
    region = country_module.model.get_region(region_code)
    if region is None:
        msg = f"Unsupported {country.upper()} region: {region_code}"
        raise ValueError(msg)
    if country == "us":
        _reject_unscoped_us_place_region(region_code, region)

    dataset_path = getattr(region, "dataset_path", None)
    if isinstance(dataset_path, str):
        bundle = get_country_release_bundle(country)
        dataset_reference = runtime_dataset_uri(
            dataset_path,
            default_revision=bundle.data_package_version,
            artifact_revision=bundle.data_artifact_revision,
            validate_hf=False,
        )
    else:
        dataset_reference = _region_parent_dataset_reference(
            country_module,
            country,
            region,
        )

    return RegionResolution(
        code=region_code,
        dataset_reference=dataset_reference,
        scoping_strategy=getattr(region, "scoping_strategy", None),
        state_code=getattr(region, "state_code", None) if country == "us" else None,
    )


def _resolve_region_group(
    *,
    country_module: ModuleType,
    country: str,
    params: dict[str, Any],
) -> RegionResolution:
    """Resolve a ``region_group`` list of region codes to one ``RegionGroupStrategy``.

    The strategy scopes a single simulation to the union of the members.
    Members must be row-filter regions (states/CDs) so national weights are
    preserved (INV-5); the group filters the national dataset.
    """
    from policyengine.core.scoping_strategy import (  # noqa: PLC0415  # this module keeps policyengine out of its module-level imports (see its docstring)
        RegionGroupStrategy,
        RowFilterStrategy,
    )

    codes = [
        _normalise_region_code(country, code)
        for code in (params.get("region_group") or [])
    ]
    if not codes:
        msg = "region_group must contain at least one region code"
        raise ValueError(msg)

    members = []
    for code in codes:
        region = country_module.model.get_region(code)
        if region is None:
            msg = f"Unsupported {country.upper()} region in group: {code}"
            raise ValueError(msg)
        scoping_strategy = getattr(region, "scoping_strategy", None)
        if not isinstance(scoping_strategy, RowFilterStrategy):
            msg = (
                f"Region group member '{code}' is not a row-filter region "
                f"(got {type(scoping_strategy).__name__}); region groups must "
                "preserve national weights."
            )
            raise ValueError(msg)  # noqa: TRY004  # an unsupported region in the request; run_simulation_impl handles ValueError specially
        members.append(scoping_strategy)

    return RegionResolution(
        code="region_group/" + "+".join(sorted(codes)),
        dataset_reference=resolve_dataset_reference(country),
        scoping_strategy=RegionGroupStrategy(members=members),
    )


def require_local_area_breakdown(params: dict[str, Any], *, country: str) -> None:
    """Refuse a local-area breakdown this worker cannot run, before loading data.

    The gateway contract applies the same request rules; they are checked
    again here because the worker can be called with any payload. The bundle
    must also route UK area regions to a local-area dataset, which every
    breakdown runs on.
    """
    breakdown = params.get("breakdown")
    if breakdown != LOCAL_AREAS_BREAKDOWN:
        msg = (
            f"Unsupported breakdown {breakdown!r}; the only breakdown is "
            f"{LOCAL_AREAS_BREAKDOWN!r}"
        )
        raise ValueError(msg)
    region = params.get("region")
    error = local_area_breakdown_error(
        country=country,
        region=None if region is None else str(region),
        region_group=params.get("region_group"),
        include_cliffs=params.get("include_cliffs"),
        segmented=params.get("segmented"),
    )
    if error is not None:
        raise ValueError(error)
    if not _uk_area_regions_routed():
        msg = (
            "This policyengine.py bundle does not route UK constituency and "
            "local-authority regions to a local-area dataset, so it cannot run "
            "a local-area breakdown"
        )
        raise ValueError(msg)
