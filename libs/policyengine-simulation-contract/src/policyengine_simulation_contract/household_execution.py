"""Versioned household calculation contracts for the v2 Modal household functions.

A household travels in the situation shape the Public API's household
calculation routes accept and return: ``people`` maps each person to their
variables, every other top-level key is a group entity collection (such as
``households`` or ``tax_units``) whose groups list their ``members``, each
variable maps periods to values, and an optional ``axes`` list varies inputs
across points. A calculated household keeps that shape, with each requested
value filled in, so a v2 result can be compared field by field with the
Public API's in-process calculation.

These models check structure, types and size only. Whether an entity
collection, variable or policy parameter exists is decided by the country
model inside the household function, which reports unknown names with the
same typed input errors as the Public API.

Wire models:

- ``HouseholdExecutionRequest``: Simulation Entrypoint to a Modal household
  function. It names one or more policies, so a baseline and its reform run in
  one invocation.
- ``HouseholdExecutionResult`` or ``HouseholdExecutionFailure``: what the
  household function returns, told apart by ``status``.
- ``SubmitHouseholdCalculationRequest``: the public submission to the
  Simulation Entrypoint.
- The poll responses ``PendingHouseholdCalculation``,
  ``RunningHouseholdCalculation``, ``SucceededHouseholdCalculation`` and
  ``FailedHouseholdCalculation``, told apart by ``status``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from policyengine_simulation_contract.spm import (
    SPMErrorDetail,
    SPMProvenance,
    SPMSelection,
)

if TYPE_CHECKING:
    from pydantic import SerializerFunctionWrapHandler

HOUSEHOLD_CONTRACT_SCHEMA_VERSION = 1
# Size limits. The entity and value limits match the Public API's stored v2
# households; the variable limit leaves room for a complete calculated
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
PolicyKey = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9_-]{0,63}$",
        description=(
            "Caller-chosen name for one policy in the request, such as "
            "'baseline' or 'reform'; each result carries the same name."
        ),
    ),
]
Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CountryId = Annotated[
    Literal["us", "uk"],
    Field(description="Supported PolicyEngine country ID"),
]

# One input value. ``None`` asks the household function to calculate it.
InputScalar = bool | int | float | str | None
PeriodInputs = Annotated[
    dict[PeriodKey, InputScalar],
    Field(max_length=MAXIMUM_PERIODS_PER_VARIABLE),
]
# A variable's values by period, or one value for the model's default period.
VariableInput = PeriodInputs | InputScalar
EntityInputs = Annotated[
    dict[VariableName, VariableInput],
    Field(max_length=MAXIMUM_VARIABLES_PER_ENTITY),
]
# One calculated value. Households with axes hold one value per point.
# Infinite and undefined numbers are the strings "Infinity", "-Infinity" and
# "NaN", as the Public API serializes infinities.
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
ParameterValue = bool | int | float | str | None

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


class HouseholdGroup(BaseModel):
    """One group entity instance, such as a household or a tax unit.

    ``members`` names the people in the group. Every other key is a variable
    mapped to its values by period, exactly as in the Public API's situation
    shape.
    """

    model_config = ConfigDict(extra="allow", allow_inf_nan=False, frozen=True)

    members: GroupMembers
    __pydantic_extra__: dict[VariableName, VariableInput] = Field(init=False)

    @model_validator(mode="after")
    def limit_variables(self) -> Self:
        """Bound the number of variables one group may carry."""
        if len(self.model_extra or {}) > MAXIMUM_VARIABLES_PER_ENTITY:
            msg = "a group carries too many variables"
            raise ValueError(msg)
        return self


class CalculatedHouseholdGroup(BaseModel):
    """One calculated group entity instance: its members and calculated variables."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False, frozen=True)

    members: GroupMembers
    __pydantic_extra__: dict[VariableName, VariableOutput] = Field(init=False)

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
    ``max`` in ``period`` (the model's default period when omitted), for the
    entity member at position ``index`` (the first when omitted).
    """

    name: VariableName
    count: Annotated[int, Field(ge=1, le=MAXIMUM_AXIS_POINTS)]
    min: float
    max: float
    period: PeriodKey | int | None = None
    index: Annotated[int, Field(ge=0)] | None = None

    @model_serializer(mode="wrap")
    def serialize_axis(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Leave out optional keys that were not given, as the Public API does."""
        dumped: dict[str, Any] = handler(self)
        for name in ("period", "index"):
            if dumped.get(name) is None and name not in self.model_fields_set:
                dumped.pop(name, None)
        return dumped


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


class _SituationModel(BaseModel):
    """Situation-shaped household: ``people``, group collections and ``axes``."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False, frozen=True)

    axes: Axes = None

    @model_validator(mode="after")
    def limit_collections(self) -> Self:
        """Bound the number of group collections a household may carry."""
        if len(self.model_extra or {}) > MAXIMUM_GROUP_COLLECTIONS:
            msg = "a household carries too many group collections"
            raise ValueError(msg)
        return self

    @model_serializer(mode="wrap")
    def serialize_situation(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Leave out ``axes`` when there are none, so the shape matches the input."""
        dumped: dict[str, Any] = handler(self)
        if self.axes is None:
            dumped.pop("axes", None)
        return dumped

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

    def group_collections(self) -> dict[str, Any]:
        """Return each group entity collection by its plural, in JSON form."""
        situation = self.to_situation()
        return {
            plural: situation[plural]
            for plural in situation
            if plural not in {"people", "axes"}
        }


class HouseholdDocument(_SituationModel):
    """A household to calculate, in the Public API's situation shape.

    ``people`` maps each person's name to their variables. Every other key
    except ``axes`` is a group entity collection, by its plural, mapping each
    group's name to a ``HouseholdGroup``. A variable maps periods (such as
    ``"2026"``) to values; ``None`` asks for the value to be calculated.
    """

    people: Annotated[
        dict[EntityKey, EntityInputs], Field(min_length=1, max_length=MAXIMUM_PEOPLE)
    ]
    __pydantic_extra__: dict[
        EntityPlural,
        Annotated[
            dict[EntityKey, HouseholdGroup],
            Field(max_length=MAXIMUM_GROUPS_PER_COLLECTION),
        ],
    ] = Field(init=False)


class CalculatedHousehold(_SituationModel):
    """A calculated household, in the shape the Public API returns as ``result``.

    It holds the request's people, groups and axes with each requested value
    filled in. With axes, a calculated variable holds one value per point.
    """

    people: Annotated[
        dict[EntityKey, EntityOutputs], Field(min_length=1, max_length=MAXIMUM_PEOPLE)
    ]
    __pydantic_extra__: dict[
        EntityPlural,
        Annotated[
            dict[EntityKey, CalculatedHouseholdGroup],
            Field(max_length=MAXIMUM_GROUPS_PER_COLLECTION),
        ],
    ] = Field(init=False)


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


def _reject_explicit_null_spm(value: object) -> object:
    # Validators do not run on the default, so only an explicit null gets here.
    if value is None:
        msg = "spm must be an object; omit it to inherit the certified defaults."
        raise ValueError(msg)
    return value


SPMChoice = Annotated[
    SPMSelection | None,
    BeforeValidator(_reject_explicit_null_spm),
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


class _HouseholdCalculationInputs(StrictHouseholdModel):
    """Fields shared by the public submission and the execution request.

    ``add_missing_variables`` adds every yearly, monthly and eternal variable
    the household's entities lack before calculating, as the Public API's
    ``/calculate-full`` route does. ``spm`` selects SPM settings for a US
    household: omit it to inherit the bundle's certified defaults; supplying
    it, even as an empty object, is a choice, and then a missing SPM input is
    an error rather than an unavailable value. ``spm_selection_source`` says
    whether the selection came from this request or from a saved household,
    which changes only how an SPM artifact the bundle no longer carries is
    reported.
    """

    country: CountryId
    household: HouseholdDocument
    policies: Policies
    add_missing_variables: bool = False
    spm: SPMChoice = Field(
        default=None,
        json_schema_extra=lambda schema: schema.pop("default", None),
    )
    spm_selection_source: Literal["request", "saved_household"] = "request"

    @model_serializer(mode="wrap")
    def serialize_inputs(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Leave out ``spm`` when no selection was made, so omission is kept."""
        dumped: dict[str, Any] = handler(self)
        if self.spm is None:
            dumped.pop("spm", None)
        return dumped

    @property
    def spm_requested(self) -> bool:
        """Whether the caller chose SPM settings rather than inheriting the defaults."""
        return self.spm is not None


class HouseholdExecutionRequest(_HouseholdCalculationInputs):
    """Simulation Entrypoint to Modal household function: calculate one household.

    ``calculation_id`` is the durable job identity. ``bundle`` is the bundle
    the entrypoint resolved from the version manifest; the household function
    refuses a request for any other bundle.
    """

    schema_version: Literal[1] = HOUSEHOLD_CONTRACT_SCHEMA_VERSION
    calculation_id: UUID
    bundle: HouseholdBundleProvenance


class SubmitHouseholdCalculationRequest(_HouseholdCalculationInputs):
    """Public submission of one household calculation to the Simulation Entrypoint.

    ``policyengine_version`` pins a PolicyEngine.py bundle; omit it to use the
    version manifest's default.
    """

    schema_version: Literal[1] = HOUSEHOLD_CONTRACT_SCHEMA_VERSION
    policyengine_version: ContractText | None = None

    def execution_request(
        self,
        *,
        calculation_id: UUID,
        bundle: HouseholdBundleProvenance,
    ) -> HouseholdExecutionRequest:
        """Return the request for the household function that runs ``bundle``.

        Raises ValueError when a pinned PolicyEngine.py version differs from
        the bundle's.
        """
        if (
            self.policyengine_version is not None
            and self.policyengine_version != bundle.policyengine_version
        ):
            msg = "the pinned PolicyEngine.py version is not the resolved bundle's"
            raise ValueError(msg)
        inputs = self.model_dump(
            mode="json",
            exclude={"schema_version", "policyengine_version"},
        )
        return HouseholdExecutionRequest.model_validate(
            {
                **inputs,
                "calculation_id": calculation_id,
                "bundle": bundle.model_dump(mode="json"),
            }
        )


class HouseholdPolicyResult(StrictHouseholdModel):
    """The calculated household under one policy.

    ``warnings`` lists, in order, the deprecated inputs that were ignored and
    the values that could not be calculated, worded as the Public API words
    them. ``spm_provenance`` is the SPM calculation receipt for a US household
    calculated with SPM settings.
    """

    policy_key: PolicyKey
    household: CalculatedHousehold
    warnings: Annotated[tuple[str, ...], Field(max_length=MAXIMUM_WARNINGS)] = ()
    spm_provenance: SPMProvenance | None = None


class HouseholdCalculationOutput(StrictHouseholdModel):
    """A finished household calculation: one result per policy, in request order."""

    country: CountryId
    results: Annotated[
        tuple[HouseholdPolicyResult, ...],
        Field(min_length=1, max_length=MAXIMUM_POLICIES_PER_REQUEST),
    ]
    bundle: HouseholdBundleProvenance

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
    list. The entity plurals are set only for a variable placed on the wrong
    entity.
    """

    type: InputErrorType
    name: str
    path: str
    message: str
    expected_entity_plural: str | None = None
    actual_entity_plural: str | None = None


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
    """Modal household function to Simulation Entrypoint: the calculation succeeded."""

    status: Literal["succeeded"] = "succeeded"
    schema_version: Literal[1] = HOUSEHOLD_CONTRACT_SCHEMA_VERSION
    calculation_id: UUID
    output: HouseholdCalculationOutput
    worker: HouseholdWorkerIdentity


class HouseholdExecutionFailure(StrictHouseholdModel):
    """Modal household function to Simulation Entrypoint: a typed failure."""

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


class PendingHouseholdCalculation(StrictHouseholdModel):
    """Poll response: the calculation is accepted and not yet running."""

    status: Literal["pending"] = "pending"
    schema_version: Literal[1] = HOUSEHOLD_CONTRACT_SCHEMA_VERSION
    job_id: UUID


class RunningHouseholdCalculation(StrictHouseholdModel):
    """Poll response: the household function is calculating."""

    status: Literal["running"] = "running"
    schema_version: Literal[1] = HOUSEHOLD_CONTRACT_SCHEMA_VERSION
    job_id: UUID


class SucceededHouseholdCalculation(StrictHouseholdModel):
    """Poll response: the calculation finished; ``output`` holds the results."""

    status: Literal["succeeded"] = "succeeded"
    schema_version: Literal[1] = HOUSEHOLD_CONTRACT_SCHEMA_VERSION
    job_id: UUID
    output: HouseholdCalculationOutput


class FailedHouseholdCalculation(StrictHouseholdModel):
    """Poll response: the calculation failed; ``error`` says why."""

    status: Literal["failed"] = "failed"
    schema_version: Literal[1] = HOUSEHOLD_CONTRACT_SCHEMA_VERSION
    job_id: UUID
    error: HouseholdCalculationError


HouseholdCalculationPollResponse = Annotated[
    PendingHouseholdCalculation
    | RunningHouseholdCalculation
    | SucceededHouseholdCalculation
    | FailedHouseholdCalculation,
    Field(discriminator="status"),
]
_POLL_RESPONSE_ADAPTER: TypeAdapter[
    PendingHouseholdCalculation
    | RunningHouseholdCalculation
    | SucceededHouseholdCalculation
    | FailedHouseholdCalculation
] = TypeAdapter(HouseholdCalculationPollResponse)


def parse_household_calculation_poll_response(
    payload: object,
) -> (
    PendingHouseholdCalculation
    | RunningHouseholdCalculation
    | SucceededHouseholdCalculation
    | FailedHouseholdCalculation
):
    """Validate a household calculation poll response by its ``status``."""
    return _POLL_RESPONSE_ADAPTER.validate_python(payload)
