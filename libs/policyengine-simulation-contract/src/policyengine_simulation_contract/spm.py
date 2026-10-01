"""Dependency-light public models for US SPM selection and calculation receipts."""

import re
from collections.abc import Sequence
from datetime import date
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)


def _selection_schema(schema):
    # An omitted option inherits the selected bundle's default. Advertising
    # Python attribute defaults here makes generated clients send an explicit
    # county selection even when their caller omitted geography entirely.
    for field in schema.get("properties", {}).values():
        field.pop("default", None)


class SPMSelection(BaseModel):
    """Select from the bundle's pinned artifact; national geography is explicit.

    County mode reads the household's observed ``county_fips``. A state alone
    does not identify an SPM area. These settings contain no provider or path.
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", json_schema_extra=_selection_schema
    )

    forecast_content_sha256: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    scenario: Optional[str] = Field(default=None, min_length=1, pattern=r"^\S+$")
    geography_kind: Literal["county", "national", "metro"] = "county"
    geography_id: Optional[str] = Field(default=None, min_length=1)
    county_vintage: str = Field(default="2020", pattern=r"^[0-9]{4}$")
    as_of: Optional[str] = None

    @model_serializer(mode="wrap")
    def serialize_selection(
        self, handler: SerializerFunctionWrapHandler, info: SerializationInfo
    ):
        """Preserve inherited options through ordinary and nested JSON.

        Presence is the contract: an omitted option inherits the bundle
        default, so an option explicitly selected as null has to stay on the
        wire. ``exclude_none`` would otherwise turn a completed result's
        resolved selection back into a partial request, and the poll routes
        apply it to every body via ``response_model_exclude_none``.
        """
        dumped = handler(self)
        if info.exclude_none:
            excluded = info.exclude or frozenset()
            dumped = {
                name: dumped.get(name)
                for name in type(self).model_fields
                if name in dumped
                or (
                    name in self.model_fields_set
                    and name not in excluded
                    and getattr(self, name) is None
                )
            }
        return {
            name: value
            for name, value in dumped.items()
            if name in self.model_fields_set
        }

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value):
        if value is not None:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("as_of must be an ISO calendar date (YYYY-MM-DD)")
        return value

    @model_validator(mode="after")
    def validate_location(self):
        # Request options inherit bundle defaults. Validate coupled options
        # once both are explicit (including after defaults are resolved).
        if not {"geography_kind", "geography_id"} <= self.model_fields_set:
            return self
        if self.geography_kind == "metro":
            if not self.geography_id or not self.geography_id.strip():
                raise ValueError("An SPM area selection requires geography_id")
        elif self.geography_id is not None:
            raise ValueError("Only an SPM area selection accepts geography_id")
        return self


class SPMProvenance(BaseModel):
    """Compact detached receipt for one SPM calculation configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, serialize_by_alias=True)

    schema_version: Literal["canonical-spm-provenance-v2"]
    forecast_id: str = Field(min_length=1, pattern=r"^\S+$")
    forecast_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario: str = Field(min_length=1, pattern=r"^\S+$")
    geography_kind: Literal["county", "national", "metro"]
    geography_id: Optional[str] = Field(min_length=1)
    county_vintage: str = Field(pattern=r"^[0-9]{4}$")
    as_of: Optional[date]
    years: tuple[str, ...]
    runtime_versions: "SPMRuntimeVersions"

    @field_validator("years")
    @classmethod
    def validate_years(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(r"[0-9]{4}", year) is None for year in value):
            raise ValueError("SPM provenance years must use four-digit years")
        if value != tuple(sorted(set(value))):
            raise ValueError("SPM provenance years must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_provenance_location(self) -> "SPMProvenance":
        if self.geography_kind == "metro":
            if self.geography_id is None or not self.geography_id.strip():
                raise ValueError("An SPM area receipt requires geography_id")
        elif self.geography_id is not None:
            raise ValueError("Only an SPM area receipt accepts geography_id")
        return self


class SPMRuntimeVersions(BaseModel):
    """Package versions needed to reproduce an SPM calculation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    policyengine: str
    policyengine_core: str = Field(alias="policyengine-core")
    policyengine_us: str = Field(alias="policyengine-us")
    spm_calculator: str = Field(alias="spm-calculator")


class SPMCalculationProvenance(BaseModel):
    """Resolved SPM selection paired with its compact calculation receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spm_config: SPMSelection
    spm_provenance: SPMProvenance

    @model_validator(mode="after")
    def require_matching_receipt(self) -> "SPMCalculationProvenance":
        _require_complete_selection(self.spm_config)
        expected = {
            "forecast_sha256": self.spm_config.forecast_content_sha256,
            "scenario": self.spm_config.scenario,
            "geography_kind": self.spm_config.geography_kind,
            "geography_id": self.spm_config.geography_id,
            "county_vintage": self.spm_config.county_vintage,
            "as_of": (
                date.fromisoformat(self.spm_config.as_of)
                if self.spm_config.as_of is not None
                else None
            ),
        }
        for field_name, expected_value in expected.items():
            if getattr(self.spm_provenance, field_name) != expected_value:
                raise ValueError(
                    f"SPM receipt {field_name} differs from the resolved selection"
                )
        return self


def _require_complete_selection(selection: SPMSelection) -> None:
    required = set(SPMSelection.model_fields)
    missing = required.difference(selection.model_fields_set)
    if missing:
        raise ValueError(
            "Resolved SPM selection is missing " + ", ".join(sorted(missing))
        )
    if selection.forecast_content_sha256 is None:
        raise ValueError("Resolved SPM selection has no artifact hash")
    if selection.scenario is None:
        raise ValueError("Resolved SPM selection has no scenario")


def build_spm_provenance(
    *,
    forecast_id: str,
    forecast_sha256: str,
    selection: SPMSelection,
    years: Sequence[str],
    runtime_versions: SPMRuntimeVersions,
) -> SPMProvenance:
    """Build one compact receipt from resolved calculation inputs."""

    _require_complete_selection(selection)
    assert selection.forecast_content_sha256 is not None
    assert selection.scenario is not None
    if forecast_sha256 != selection.forecast_content_sha256:
        raise ValueError("SPM receipt artifact hash differs from the selection")
    return SPMProvenance(
        schema_version="canonical-spm-provenance-v2",
        forecast_id=forecast_id,
        forecast_sha256=forecast_sha256,
        scenario=selection.scenario,
        geography_kind=selection.geography_kind,
        geography_id=selection.geography_id,
        county_vintage=selection.county_vintage,
        as_of=(
            date.fromisoformat(selection.as_of) if selection.as_of is not None else None
        ),
        years=tuple(years),
        runtime_versions=runtime_versions,
    )


def build_spm_calculation_provenance(
    *,
    config: SPMSelection,
    receipt: SPMProvenance,
) -> SPMCalculationProvenance:
    """Pair a resolved selection with a matching compact receipt."""

    return SPMCalculationProvenance(spm_config=config, spm_provenance=receipt)


SPM_CONTRACT_VERSION = "canonical-spm-v1"
SPM_ERROR_CODES = frozenset(
    {
        "SPM_GEOGRAPHY_REQUIRED",
        "SPM_GEOGRAPHY_UNAVAILABLE",
        "SPM_COMPOSITION_REQUIRED",
        "SPM_YEAR_UNAVAILABLE",
        "SPM_SCENARIO_UNAVAILABLE",
        "SPM_CONFIGURATION_UNAVAILABLE",
        "SPM_SETTINGS_INVALID",
    }
)


class SPMErrorDetail(BaseModel):
    code: str
    message: str


class SPMInputError(ValueError):
    """Transportable error shared by the control plane and worker."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(code, message)

    def __str__(self):
        return self.message

    def to_dict(self):
        return {"code": self.code, "message": self.message}


def spm_error_detail(exc: BaseException) -> SPMErrorDetail | None:
    """Recognize only public typed errors, including wrapped country errors."""
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "code", None)
        if isinstance(current, ValueError) and code in SPM_ERROR_CODES:
            return SPMErrorDetail(code=code, message=str(current))
        current = current.__cause__ or current.__context__
    return None


class SPMCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal["canonical-spm-v1"] = SPM_CONTRACT_VERSION
    defaults: SPMSelection

    @model_validator(mode="after")
    def pinned_defaults(self):
        if not self.defaults.forecast_content_sha256 or not self.defaults.scenario:
            raise ValueError(
                "Certified SPM defaults must pin artifact hash and scenario"
            )
        self.defaults = SPMSelection(
            **{name: getattr(self.defaults, name) for name in SPMSelection.model_fields}
        )
        return self


class SPMExecutionProvenance(BaseModel):
    """One shared receipt and the number of executions that produced it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt: SPMProvenance
    execution_count: int = Field(ge=1)


class SPMComparisonProvenance(BaseModel):
    """Compact receipts for the baseline and reform execution sets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["canonical-spm-comparison-v2"]
    baseline: SPMExecutionProvenance
    reform: SPMExecutionProvenance


def _collapse_spm_receipts(
    side: str,
    receipts: Sequence[SPMProvenance],
) -> SPMExecutionProvenance:
    if not receipts:
        raise ValueError(f"{side} receipts must not be empty")
    first = receipts[0]
    if any(receipt != first for receipt in receipts[1:]):
        raise ValueError(f"{side} receipts differ")
    return SPMExecutionProvenance(receipt=first, execution_count=len(receipts))


def build_spm_comparison_provenance(
    *,
    baseline_receipts: Sequence[SPMProvenance],
    reform_receipts: Sequence[SPMProvenance],
) -> SPMComparisonProvenance:
    """Collapse identical child receipts into one receipt per policy side."""

    baseline = _collapse_spm_receipts("baseline", baseline_receipts)
    reform = _collapse_spm_receipts("reform", reform_receipts)
    if baseline.receipt != reform.receipt:
        raise ValueError("baseline and reform SPM receipts differ")
    return SPMComparisonProvenance(
        schema_version="canonical-spm-comparison-v2",
        baseline=baseline,
        reform=reform,
    )


def resolve_spm_selection(
    country,
    selection,
    *,
    capability,
    policyengine_version,
    model_version,
    route_provenance=None,
):
    """Resolve only certified metadata; unrecognized future bundles fail closed."""
    if country.lower() != "us":
        if selection is not None:
            raise SPMInputError(
                "SPM_SETTINGS_INVALID", "SPM settings are supported only for the US"
            )
        return None
    if capability is None:
        # Existing versioned worker bundles predate canonical SPM. This does
        # not certify a future bundle or change the routing registry default.
        version_parts = str(policyengine_version or "").split(".")
        historical = (
            len(version_parts) == 3
            and all(re.fullmatch(r"[0-9]+", p) for p in version_parts)
            and tuple(map(int, version_parts)) < (5, 2, 0)
        )
        # The last two pre-canonical wrappers pin US 1.764.6. A route the
        # registry carries no manifest for states no model version at all,
        # and states nothing that contradicts the wrapper pin; a route that
        # states a different model version does.
        historical = historical or (
            policyengine_version in {"5.2.0", "5.3.0"}
            and model_version in (None, "1.764.6")
        )
        # Country-only routes predating the bundle manifests have no wrapper
        # version. Require both their route provenance -- derived from route
        # shape, not from the registry's rewritable generation marker -- and
        # a pre-canonical US model; absence of capability alone proves
        # nothing.
        model_parts = str(model_version or "").split(".")
        historical = historical or (
            policyengine_version is None
            and route_provenance in {"legacy-country-dict", "legacy-country-route"}
            and len(model_parts) == 3
            and all(re.fullmatch(r"[0-9]+", p) for p in model_parts)
            and tuple(map(int, model_parts)) <= (1, 764, 6)
        )
        if selection is None and historical:
            return None
        raise SPMInputError(
            "SPM_CONFIGURATION_UNAVAILABLE",
            "This worker bundle has no certified canonical SPM capability",
        )
    try:
        cap = SPMCapability.model_validate(capability)
        chosen = SPMSelection.model_validate({} if selection is None else selection)
        defaults = cap.defaults
        if chosen.forecast_content_sha256 not in (
            None,
            defaults.forecast_content_sha256,
        ):
            raise ValueError(
                "SPM selection does not match the certified bundle artifact hash"
            )
        values = {name: getattr(defaults, name) for name in SPMSelection.model_fields}
        values.update(chosen.model_dump(exclude_unset=True))
        if (
            "geography_kind" in chosen.model_fields_set
            and chosen.geography_kind != defaults.geography_kind
        ):
            values["geography_id"] = chosen.geography_id
        values["forecast_content_sha256"] = defaults.forecast_content_sha256
        values["scenario"] = chosen.scenario or defaults.scenario
        return SPMSelection.model_validate(values).model_dump(mode="json")
    except ValueError as exc:
        raise SPMInputError("SPM_SETTINGS_INVALID", str(exc)) from exc


def validate_spm_result(
    result: dict, selection: Any, *, expected_year: int | str | None = None
):
    """Do not accept incomplete or mixed-method child/cached output."""
    if selection is None:
        if (
            result.get("spm_config") is not None
            or result.get("spm_provenance") is not None
        ):
            raise SPMInputError(
                "SPM_CONFIGURATION_UNAVAILABLE",
                "Unexpected SPM receipt for a historical result",
            )
        return None
    try:
        chosen = SPMSelection.model_validate(selection)
        result_selection = SPMSelection.model_validate(result.get("spm_config"))
        required = {
            "forecast_content_sha256",
            "scenario",
            "geography_kind",
            "county_vintage",
        }
        if not required <= result_selection.model_fields_set:
            raise ValueError("Result has no complete resolved SPM selection")
        selection = {name: getattr(chosen, name) for name in SPMSelection.model_fields}
        if {
            name: getattr(result_selection, name) for name in SPMSelection.model_fields
        } != selection:
            raise ValueError("Result SPM selection differs from the request")
        provenance = SPMComparisonProvenance.model_validate(
            result.get("spm_provenance")
        )
        for execution in (provenance.baseline, provenance.reform):
            build_spm_calculation_provenance(
                config=chosen,
                receipt=execution.receipt,
            )
            if (
                expected_year is not None
                and str(expected_year) not in execution.receipt.years
            ):
                raise ValueError(
                    "Result SPM provenance does not cover the requested year"
                )
        return provenance
    except ValueError as exc:
        raise SPMInputError("SPM_CONFIGURATION_UNAVAILABLE", str(exc)) from exc


def combine_spm_results(
    results: list[dict],
    selection: dict | None,
    *,
    expected_year: int | str | None = None,
) -> dict:
    receipts = [
        validate_spm_result(result, selection, expected_year=expected_year)
        for result in results
    ]
    if selection is None:
        return {}
    validated: list[SPMComparisonProvenance] = []
    for receipt in receipts:
        if receipt is None:
            raise SPMInputError(
                "SPM_CONFIGURATION_UNAVAILABLE", "Missing canonical SPM receipt"
            )
        validated.append(receipt)
    combined = build_spm_comparison_provenance(
        baseline_receipts=[
            item.baseline.receipt
            for item in validated
            for _ in range(item.baseline.execution_count)
        ],
        reform_receipts=[
            item.reform.receipt
            for item in validated
            for _ in range(item.reform.execution_count)
        ],
    )
    return {
        "spm_config": selection,
        "spm_provenance": combined.model_dump(mode="json", by_alias=True),
    }
