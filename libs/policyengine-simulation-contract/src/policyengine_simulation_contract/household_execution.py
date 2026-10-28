"""Versioned household calculation contract for the v2 Modal household functions.

A household travels in the situation shape the Public API's household
calculation routes accept and return: ``people`` maps each person to their
variables, every other top-level key except ``axes`` is a group entity
collection (such as ``households`` or ``tax_units``) whose groups list their
``members``, each variable maps periods to values, and an optional ``axes``
list varies inputs across points. A calculated household keeps that shape,
with each requested value filled in, so a v2 result can be compared field by
field with the Public API's in-process calculation.

These models check structure, types and size only. Whether an entity
collection, variable or policy parameter exists is decided by the country
model inside the household function, which reports unknown names with the
same input errors as the Public API.

Wire models:

- ``HouseholdExecutionRequest``: the caller to a Modal household function. It
  names one to ten policies, so a baseline and its reform are calculated in
  one call.
- ``HouseholdExecutionResult`` or ``HouseholdExecutionFailure``: what the
  household function returns, told apart by ``status``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from policyengine_simulation_contract.spm import (
    SPMErrorDetail,
    SPMProvenance,
    SPMResolvedConfiguration,
)
from policyengine_simulation_contract.stage12_bundle import (
    CountryId,
    Sha256Digest,
    Stage12BundleManifest,
)

if TYPE_CHECKING:
    from pydantic import ModelWrapValidatorHandler, SerializerFunctionWrapHandler

HOUSEHOLD_CONTRACT_SCHEMA_VERSION = 1
# A v2 worker application's household function for a country is this prefix
# followed by the country ID, such as ``calculate_household_us``.
HOUSEHOLD_CALLABLE_PREFIX = "calculate_household_"
# Size limits. The variable limit leaves room for a complete calculated
# household sent back as input, which holds every variable of each entity.
MAXIMUM_PEOPLE = 1_000
MAXIMUM_GROUPS_PER_COLLECTION = 1_000
MAXIMUM_GROUP_COLLECTIONS = 32
MAXIMUM_VARIABLES_PER_ENTITY = 10_000
MAXIMUM_PERIODS_PER_VARIABLE = 1_000
MAXIMUM_AXIS_GROUPS = 8
MAXIMUM_PARALLEL_AXES = 32
# The number of points a household is calculated at: the product of the point
# counts of its perpendicular axis groups.
MAXIMUM_AXIS_POINTS = 10_000
MAXIMUM_POLICIES_PER_REQUEST = 10
MAXIMUM_POLICY_PARAMETERS = 10_000
MAXIMUM_WARNINGS = 10_000

EntityKey = Annotated[str, Field(min_length=1, max_length=255)]
EntityPlural = Annotated[str, Field(min_length=1, max_length=255)]
VariableName = Annotated[str, Field(min_length=1, max_length=255)]
PeriodKey = Annotated[str, Field(min_length=1, max_length=64)]
ParameterPath = Annotated[str, Field(min_length=1, max_length=512)]
ParameterPeriod = Annotated[str, Field(min_length=1, max_length=64)]
ContractText = Annotated[str, Field(min_length=1, max_length=255)]
CalculationYear = Annotated[
    str,
    Field(
        pattern=r"^[0-9]{4}$",
        description=(
            "The run's year, written as a situation's period key, such as "
            "'2025': the period of added variables and of values and axes "
            "the household gives without one."
        ),
    ),
]
PolicyKey = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9_-]{0,63}$",
        description=(
            "Caller-chosen name for one policy in the request, such as "
            "'baseline' or 'reform'; its result carries the same name."
        ),
    ),
]

# One input value. ``None`` asks the household function to calculate it.
InputScalar = bool | int | float | str | None
PeriodInputs = Annotated[
    dict[PeriodKey, InputScalar],
    Field(max_length=MAXIMUM_PERIODS_PER_VARIABLE),
]
# A variable's values by period, or one value, for the request's year.
VariableInput = PeriodInputs | InputScalar
EntityInputs = Annotated[
    dict[VariableName, VariableInput],
    Field(max_length=MAXIMUM_VARIABLES_PER_ENTITY),
]
# One calculated value. A household with axes holds one value per point.
# Infinite and undefined numbers are the strings "Infinity", "-Infinity" and
# "NaN"; the Public API writes infinities the same way.
OutputScalar = bool | int | float | str | None
OutputValue = OutputScalar | list[OutputScalar]
PeriodOutputs = Annotated[
    dict[PeriodKey, OutputValue],
    Field(max_length=MAXIMUM_PERIODS_PER_VARIABLE),
]
VariableOutput = PeriodOutputs | OutputScalar
EntityOutputs = Annotated[
    dict[VariableName, VariableOutput],
    Field(max_length=MAXIMUM_VARIABLES_PER_ENTITY),
]
ParameterScalar = bool | int | float | str | None
# A list value sets a parameter whose value is a list, such as a list of
# variable names.
ParameterValue = ParameterScalar | list[ParameterScalar]

HouseholdErrorCode = Literal[
    # A household entity collection, variable, axis variable or policy
    # parameter the country model does not have. ``input_errors`` lists them.
    "invalid_inputs",
    # The country model could not build a simulation from the household.
    "invalid_household",
    # The SPM selection, or the SPM configuration it needs, is unusable.
    # ``spm_error`` carries the SPM error code.
    "invalid_spm_selection",
    # The request names a bundle other than the one this function runs.
    "bundle_mismatch",
    # Any other failure while calculating.
    "calculation_failed",
]
InputErrorType = Literal[
    "household_entity",
    "household_variable",
    "household_variable_wrong_entity",
    "household_axis_variable",
    "policy_parameter",
]


class StrictHouseholdModel(BaseModel):
    """Base for household contracts; rejects unknown fields and non-finite numbers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class CallerOrderedModel(BaseModel):
    """A model that serializes its top-level keys in the order its caller gave them.

    The Public API walks a household in the order the caller sent it, so the
    order of unrecognized inputs, warnings and calculated values depends on
    it. Pydantic itself would write the declared fields before the extra ones.
    Keys the caller did not give follow, in Pydantic's order.
    """

    model_config = ConfigDict(extra="allow", allow_inf_nan=False, frozen=True)

    _caller_key_order: tuple[str, ...] = PrivateAttr(default=())

    @model_validator(mode="wrap")
    @classmethod
    def remember_caller_key_order(
        cls, data: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        """Record the order of the caller's top-level keys for serialization."""
        validated: Self = handler(data)
        if isinstance(data, Mapping):
            validated._caller_key_order = tuple(str(key) for key in data)
        elif isinstance(data, CallerOrderedModel):
            validated._caller_key_order = data.caller_key_order
        return validated

    @model_serializer(mode="wrap")
    def serialize_in_caller_order(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Return the caller's keys in the caller's order, then any others."""
        dumped: dict[str, Any] = handler(self)
        ordered = {key: dumped[key] for key in self._caller_key_order if key in dumped}
        ordered.update(dumped)
        left_out = self.keys_left_out()
        return {key: value for key, value in ordered.items() if key not in left_out}

    @property
    def caller_key_order(self) -> tuple[str, ...]:
        """The caller's top-level keys, in the order the caller gave them."""
        return self._caller_key_order

    def keys_left_out(self) -> frozenset[str]:
        """Return the keys serialization leaves out; none unless a subclass says."""
        return frozenset()


def _require_unique_members(members: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(members)) != len(members):
        msg = "group members must be unique"
        raise ValueError(msg)
    return members


GroupMembers = Annotated[
    tuple[EntityKey, ...],
    Field(min_length=1, max_length=MAXIMUM_PEOPLE),
    AfterValidator(_require_unique_members),
]


class HouseholdGroup(CallerOrderedModel):
    """One group entity instance, such as a household or a tax unit.

    ``members`` names the people in the group. Every other key is a variable
    mapped to its values by period, exactly as in the Public API's situation
    shape.
    """

    members: GroupMembers
    __pydantic_extra__: dict[VariableName, VariableInput] = Field(init=False)  # pyright: ignore[reportIncompatibleVariableOverride]  # Pydantic's documented way to type extra fields

    @model_validator(mode="after")
    def limit_variables(self) -> Self:
        """Bound the number of variables one group may carry."""
        if len(self.model_extra or {}) > MAXIMUM_VARIABLES_PER_ENTITY:
            msg = "a group carries too many variables"
            raise ValueError(msg)
        return self


class CalculatedHouseholdGroup(CallerOrderedModel):
    """One calculated group entity instance: its members and calculated variables."""

    members: GroupMembers
    __pydantic_extra__: dict[VariableName, VariableOutput] = Field(init=False)  # pyright: ignore[reportIncompatibleVariableOverride]  # Pydantic's documented way to type extra fields

    @model_validator(mode="after")
    def limit_variables(self) -> Self:
        """Bound the number of variables one group may carry."""
        if len(self.model_extra or {}) > MAXIMUM_VARIABLES_PER_ENTITY:
            msg = "a group carries too many variables"
            raise ValueError(msg)
        return self


class HouseholdAxis(StrictHouseholdModel):
    """One input varied across points, as policyengine-core reads an axis.

    The variable ``name`` takes ``count`` evenly spaced values from ``min`` to
    ``max`` in ``period`` (the request's year when omitted), for the
    entity member at position ``index`` (the first when omitted).
    """

    name: VariableName
    count: Annotated[int, Field(ge=1, le=MAXIMUM_AXIS_POINTS)]
    min: int | float
    max: int | float
    period: PeriodKey | int | None = None
    index: Annotated[int, Field(ge=0)] | None = None

    @model_serializer(mode="wrap")
    def serialize_axis(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Leave out the optional keys the caller did not give."""
        dumped: dict[str, Any] = handler(self)
        return {
            name: value
            for name, value in dumped.items()
            if name not in {"period", "index"} or name in self.model_fields_set
        }


AxisGroup = Annotated[
    tuple[HouseholdAxis, ...],
    Field(min_length=1, max_length=MAXIMUM_PARALLEL_AXES),
]


def _axis_point_count(axes: tuple[tuple[HouseholdAxis, ...], ...] | None) -> int:
    if not axes:
        return 1
    return math.prod(group[0].count for group in axes)


def _validated_axes(
    axes: tuple[tuple[HouseholdAxis, ...], ...] | None,
) -> tuple[tuple[HouseholdAxis, ...], ...] | None:
    if axes is None:
        return None
    for group in axes:
        if len({axis.count for axis in group}) != 1:
            msg = "parallel axes in one group must share one point count"
            raise ValueError(msg)
    if _axis_point_count(axes) > MAXIMUM_AXIS_POINTS:
        msg = (
            "a household may be calculated at no more than "
            f"{MAXIMUM_AXIS_POINTS} axis points"
        )
        raise ValueError(msg)
    return axes


Axes = Annotated[
    tuple[AxisGroup, ...] | None,
    Field(max_length=MAXIMUM_AXIS_GROUPS),
    AfterValidator(_validated_axes),
]


# One group entity collection: each group of one entity, by the group's name.
InputGroups = Annotated[
    dict[EntityKey, HouseholdGroup], Field(max_length=MAXIMUM_GROUPS_PER_COLLECTION)
]
CalculatedGroups = Annotated[
    dict[EntityKey, CalculatedHouseholdGroup],
    Field(max_length=MAXIMUM_GROUPS_PER_COLLECTION),
]


class _Situation(CallerOrderedModel):
    """A situation-shaped household: ``people``, group collections and ``axes``."""

    axes: Axes = None

    @model_validator(mode="after")
    def limit_collections(self) -> Self:
        """Bound the number of group collections a household may carry."""
        if len(self.model_extra or {}) > MAXIMUM_GROUP_COLLECTIONS:
            msg = "a household carries too many group collections"
            raise ValueError(msg)
        return self

    def keys_left_out(self) -> frozenset[str]:
        """Leave out ``axes`` when the household has none."""
        return frozenset({"axes"}) if self.axes is None else frozenset()

    @property
    def has_axes(self) -> bool:
        """Whether the household is calculated at more than one set of inputs."""
        return self.axes is not None

    def axis_point_count(self) -> int:
        """Return how many points the household is calculated at (1 without axes)."""
        return _axis_point_count(self.axes)

    def to_situation(self) -> dict[str, Any]:
        """Return the household as the JSON situation a country model reads."""
        return self.model_dump(mode="json")


class HouseholdDocument(_Situation):
    """A household to calculate, in the Public API's situation shape.

    ``people`` maps each person's name to their variables. Every other key
    except ``axes`` is a group entity collection, by its plural, mapping each
    group's name to a ``HouseholdGroup``. A variable maps periods (such as
    ``"2026"``) to values; ``None`` asks for the value to be calculated.
    """

    people: Annotated[
        dict[EntityKey, EntityInputs], Field(min_length=1, max_length=MAXIMUM_PEOPLE)
    ]
    __pydantic_extra__: dict[EntityPlural, InputGroups] = Field(init=False)  # pyright: ignore[reportIncompatibleVariableOverride]  # Pydantic's documented way to type extra fields


class CalculatedHousehold(_Situation):
    """A calculated household, in the shape the Public API returns as ``result``.

    It holds the request's people, groups and axes with each requested value
    filled in. With axes, a calculated variable holds one value per point.
    """

    people: Annotated[
        dict[EntityKey, EntityOutputs], Field(min_length=1, max_length=MAXIMUM_PEOPLE)
    ]
    __pydantic_extra__: dict[EntityPlural, CalculatedGroups] = Field(init=False)  # pyright: ignore[reportIncompatibleVariableOverride]  # Pydantic's documented way to type extra fields


class HouseholdPolicy(StrictHouseholdModel):
    """One policy to calculate the household under.

    ``parameters`` maps each changed parameter path to its values by period
    range (``"YYYY-MM-DD.YYYY-MM-DD"``), as in the Public API's policy JSON.
    Empty parameters mean current law.
    """

    policy_key: PolicyKey
    parameters: Annotated[
        dict[ParameterPath, dict[ParameterPeriod, ParameterValue]],
        Field(max_length=MAXIMUM_POLICY_PARAMETERS),
    ]


def _unique_policy_keys(
    policies: tuple[HouseholdPolicy, ...],
) -> tuple[HouseholdPolicy, ...]:
    keys = [policy.policy_key for policy in policies]
    if len(set(keys)) != len(keys):
        msg = "policy keys must be unique within a request"
        raise ValueError(msg)
    return policies


Policies = Annotated[
    tuple[HouseholdPolicy, ...],
    Field(min_length=1, max_length=MAXIMUM_POLICIES_PER_REQUEST),
    AfterValidator(_unique_policy_keys),
]


class HouseholdBundleProvenance(StrictHouseholdModel):
    """The PolicyEngine.py bundle a household calculation runs with.

    Names the PolicyEngine.py and policyengine-core versions, the country model
    package and version, and the SHA-256 of the normalized bundle manifest.
    """

    policyengine_version: ContractText
    core_package_version: ContractText
    country_package_name: ContractText
    country_package_version: ContractText
    bundle_manifest_sha256: Sha256Digest

    @classmethod
    def from_bundle(
        cls,
        bundle: Stage12BundleManifest,
        *,
        bundle_manifest_sha256: str,
        country: str,
    ) -> Self:
        """Return ``country``'s provenance within a normalized bundle manifest.

        Raises ValueError when the bundle does not carry ``country``.
        """
        country_bundle = next(
            (item for item in bundle.countries if item.country == country), None
        )
        if country_bundle is None:
            msg = f"the bundle does not carry country {country!r}"
            raise ValueError(msg)
        return cls(
            policyengine_version=bundle.policyengine_version,
            core_package_version=bundle.core_package_version,
            country_package_name=country_bundle.country_package_name,
            country_package_version=country_bundle.country_package_version,
            bundle_manifest_sha256=bundle_manifest_sha256,
        )


def household_callable_name(country: str) -> str:
    """Return the name of a v2 worker application's household function."""
    return f"{HOUSEHOLD_CALLABLE_PREFIX}{country}"


class HouseholdExecutionRequest(StrictHouseholdModel):
    """A caller to a Modal household function: calculate one household.

    ``calculation_id`` is the caller's durable identity for the calculation.
    ``year`` is the run's year, such as a stored household's default year,
    written as a situation's period key (four digits): the period of the
    variables ``add_missing_variables`` adds and of any value or axis the
    household gives without a period. Every other value is calculated for the
    period the household names. ``bundle`` is the bundle the caller resolved
    from the version manifest; the household function refuses a request for
    any other bundle.

    ``add_missing_variables`` adds every yearly, monthly and eternal variable
    the household's entities lack, for ``year``, before calculating, as the
    Public API's ``/calculate-full`` route does (that route takes its year from
    the first age period of a person named ``you``, or else the current year,
    and v2 households name people by record ID). ``spm`` carries the caller's
    SPM settings exactly as sent, as any JSON value, and the household
    function checks them with the Public API's SPM error codes: settings for a
    country other than the US are unsupported, and for the US an explicit null
    or malformed settings are invalid. Omit ``spm`` to inherit the bundle's
    certified defaults; sending it, even as an empty object, is a choice, and
    then a missing SPM input is an error rather than an uncalculated value.
    ``spm_selection_source`` says whether the settings came from this request
    or from a saved household, which changes only how an SPM forecast the
    bundle no longer carries is reported.

    To calculate a saved household with the inputs the Public API's
    stored-household route (``GET /{country}/household/{id}/policy/{policy_id}``)
    uses, set ``add_missing_variables``, because that route always adds
    missing variables, and ``year`` to the year the run calculates, such as
    the household's default year; pass the household's saved SPM selection as
    ``spm`` only if it has one (a saved null is no choice); and set
    ``spm_selection_source`` to ``"saved_household"``. When ``year`` is the
    year that route would infer and the household gives every value and axis
    a period, the calculated household, warnings, SPM receipt and SPM error
    codes then match that route's (the executor's household calculation module
    lists every difference). Its error messages do not: the route reports a
    household it
    cannot read, and any other failure except an SPM error, as an error
    calculating that household ID under that policy ID, while a household
    function reports the request routes' wording (``invalid_household`` and
    ``calculation_failed``), so a caller that serves the stored-household
    route rewrites those two messages.

    These models check the request's structure; a request they reject (for
    example a group without ``members``) is the caller's to report.
    """

    schema_version: Literal[1] = HOUSEHOLD_CONTRACT_SCHEMA_VERSION
    calculation_id: UUID
    country: CountryId
    year: CalculationYear
    household: HouseholdDocument
    policies: Policies
    add_missing_variables: bool = False
    spm: JsonValue = None
    spm_selection_source: Literal["request", "saved_household"] = "request"
    bundle: HouseholdBundleProvenance

    @model_serializer(mode="wrap")
    def serialize_request(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Leave out ``spm`` when the caller sent none, so omission is kept."""
        dumped: dict[str, Any] = handler(self)
        if not self.spm_requested:
            dumped.pop("spm", None)
        return dumped

    @property
    def spm_requested(self) -> bool:
        """Whether the caller sent SPM settings rather than inheriting the defaults."""
        return "spm" in self.model_fields_set


class HouseholdPolicyResult(StrictHouseholdModel):
    """The calculated household under one policy.

    ``warnings`` lists, in order, the deprecated inputs that were ignored and,
    for a household with axes, the values that could not be calculated, worded
    as the Public API words them. ``spm_provenance`` is the SPM calculation
    receipt of a US household calculated with SPM settings.
    """

    policy_key: PolicyKey
    household: CalculatedHousehold
    warnings: Annotated[tuple[str, ...], Field(max_length=MAXIMUM_WARNINGS)] = ()
    spm_provenance: SPMProvenance | None = None


class HouseholdCalculationOutput(StrictHouseholdModel):
    """A finished household calculation: one result per policy, in request order.

    ``spm_config`` is the complete SPM selection the calculation applied: the
    caller's choice merged over the bundle's certified defaults, or None when
    no SPM applies (any country but the US).
    """

    country: CountryId
    bundle: HouseholdBundleProvenance
    spm_config: SPMResolvedConfiguration | None = None
    results: Annotated[
        tuple[HouseholdPolicyResult, ...],
        Field(min_length=1, max_length=MAXIMUM_POLICIES_PER_REQUEST),
    ]

    @field_validator("results")
    @classmethod
    def require_unique_policy_keys(
        cls, results: tuple[HouseholdPolicyResult, ...]
    ) -> tuple[HouseholdPolicyResult, ...]:
        """Reject results that repeat a policy key."""
        keys = [result.policy_key for result in results]
        if len(set(keys)) != len(keys):
            msg = "result policy keys must be unique"
            raise ValueError(msg)
        return results


class HouseholdInputError(StrictHouseholdModel):
    """One household or policy name the country model does not recognize.

    Field names and messages match the entries of the Public API's ``errors``
    list, which name entity plurals only for a variable placed on the wrong
    entity.
    """

    type: InputErrorType
    name: str
    path: str
    message: str
    expected_entity_plural: str | None = None
    actual_entity_plural: str | None = None

    @model_serializer(mode="wrap")
    def serialize_input_error(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Leave out unset entity plurals, as the Public API's error entries do."""
        dumped: dict[str, Any] = handler(self)
        for name in ("expected_entity_plural", "actual_entity_plural"):
            if dumped.get(name) is None:
                dumped.pop(name, None)
        return dumped


class HouseholdCalculationError(StrictHouseholdModel):
    """Why a household calculation failed.

    ``message`` is worded as the Public API words the matching error response.
    ``input_errors`` lists unrecognized names for ``invalid_inputs``;
    ``spm_error`` holds the SPM error code for ``invalid_spm_selection``;
    ``policy_key`` names the policy being prepared or calculated, when the
    failure belongs to one.
    """

    code: HouseholdErrorCode
    message: Annotated[str, Field(min_length=1)]
    input_errors: tuple[HouseholdInputError, ...] = ()
    spm_error: SPMErrorDetail | None = None
    policy_key: PolicyKey | None = None


class HouseholdWorkerIdentity(StrictHouseholdModel):
    """The deployed Modal application and function that ran the calculation."""

    application_name: ContractText
    function_name: ContractText


class HouseholdExecutionResult(StrictHouseholdModel):
    """A Modal household function to its caller: the calculation succeeded."""

    status: Literal["succeeded"] = "succeeded"
    schema_version: Literal[1] = HOUSEHOLD_CONTRACT_SCHEMA_VERSION
    calculation_id: UUID
    output: HouseholdCalculationOutput
    worker: HouseholdWorkerIdentity


class HouseholdExecutionFailure(StrictHouseholdModel):
    """A Modal household function to its caller: the calculation failed."""

    status: Literal["failed"] = "failed"
    schema_version: Literal[1] = HOUSEHOLD_CONTRACT_SCHEMA_VERSION
    calculation_id: UUID
    error: HouseholdCalculationError
    worker: HouseholdWorkerIdentity


HouseholdExecutionOutcome = Annotated[
    HouseholdExecutionResult | HouseholdExecutionFailure,
    Field(discriminator="status"),
]
_EXECUTION_OUTCOME_ADAPTER: TypeAdapter[
    HouseholdExecutionResult | HouseholdExecutionFailure
] = TypeAdapter(HouseholdExecutionOutcome)


def parse_household_execution_outcome(
    payload: object,
) -> HouseholdExecutionResult | HouseholdExecutionFailure:
    """Validate a household function's return value as a success or a failure."""
    return _EXECUTION_OUTCOME_ADAPTER.validate_python(payload)
