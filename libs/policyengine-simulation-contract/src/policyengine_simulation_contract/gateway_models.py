"""Pydantic models for the Gateway API."""

import json
from typing import ClassVar, Literal

from policyengine_simulation_observability.telemetry import TelemetryEnvelope
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

from policyengine_simulation_contract.job_progress import (
    JobSegmentProgress,
    JobStage,
)
from policyengine_simulation_contract.json_types import JsonObject
from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.spm import (
    SPMCapability,
    SPMComparisonProvenance,
    SPMErrorDetail,
    SPMSelection,
)

# Hard cap on request body size (bytes). SimulationOptions + telemetry + any
# reform/baseline parameter tree should fit comfortably in ~256 KB. A hostile
# client that tries to stream a multi-MB reform dict is rejected with 422
# before Pydantic allocates proxy objects, which prevents memory-based DoS
# against the gateway ASGI worker. If we discover a legitimate use case that
# exceeds this we should add an explicit endpoint for bulk parameter upload
# rather than relaxing the cap here.
MAX_GATEWAY_REQUEST_BYTES = 262_144


INTERNAL_PASSTHROUGH_FIELDS = frozenset({"_metadata", "_runtime_bundle"})


class PolicyParameterChanges(RootModel[JsonObject]):
    """Policy parameter names mapped to their dated values."""


def _move_internal_telemetry_alias(value: object) -> object:
    if not isinstance(value, dict):
        return value
    if "_telemetry" in value and "telemetry" not in value:
        value = dict(value)
        value["telemetry"] = value.pop("_telemetry")
    return value


def _strip_internal_passthrough_fields(value: object) -> object:
    """Drop internal-only fields the gateway adds to payloads in flight.

    Parent batch entrypoints attach ``_metadata`` describing resolved routing,
    and the v4 API attaches ``_runtime_bundle`` describing provenance that the
    gateway returns separately. Strip those fields before strict validation so
    ``extra="forbid"`` keeps catching unknown caller fields without breaking
    internal round-trips.
    """
    if not isinstance(value, dict):
        return value
    if not INTERNAL_PASSTHROUGH_FIELDS.intersection(value):
        return value
    return {
        key: item
        for key, item in value.items()
        if key not in INTERNAL_PASSTHROUGH_FIELDS
    }


def _enforce_max_payload_size(value: object) -> object:
    if not isinstance(value, dict):
        return value
    try:
        encoded_length = len(json.dumps(value, default=str))
    except TypeError:
        # Non-JSON-serialisable values will fail later in Pydantic; size cap
        # only guards against well-formed hostile payloads.
        return value
    if encoded_length > MAX_GATEWAY_REQUEST_BYTES:
        msg = (
            f"Request body is too large ({encoded_length} bytes); the gateway "
            f"accepts at most {MAX_GATEWAY_REQUEST_BYTES} bytes."
        )
        raise ValueError(msg)
    return value


class GatewayRequestBase(BaseModel):
    """Base request model with strict passthrough of documented fields only.

    All fields that a caller may legitimately supply are declared explicitly:
    fields consumed by the gateway router (``country``, ``version``,
    ``telemetry``) plus every field the downstream ``SimulationOptions``
    worker model accepts. Unknown fields are rejected (``extra="forbid"``)
    so typos and adversarial payloads fail fast with a 422 instead of being
    forwarded opaquely to the worker app.
    """

    country: str
    spm: SPMSelection | None = None
    version: str | None = None
    policyengine_version: str | None = None
    telemetry: TelemetryEnvelope | None = None

    # Fields forwarded to SimulationOptions on the worker side.
    scope: str | None = None
    time_period: str | None = None
    reform: PolicyParameterChanges | None = None
    baseline: PolicyParameterChanges | None = None
    region_group: list[str] | None = None
    title: str | None = None
    include_cliffs: bool | None = None
    model_version: str | None = None
    # National macro requests run segmented across region groups by default;
    # False forces a monolithic single-process run. Ignored for requests that
    # are not eligible for segmentation (regional, household-scope, UK).
    segmented: bool | None = None

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    @model_validator(mode="before")
    @classmethod
    def move_internal_telemetry_alias(cls, value: object) -> object:
        """Accept the internal ``_telemetry`` key as ``telemetry`` before validation.

        ``telemetry`` wins when a payload carries both keys.
        """
        return _move_internal_telemetry_alias(value)

    @model_validator(mode="before")
    @classmethod
    def strip_internal_passthrough_fields(cls, value: object) -> object:
        """Drop ``_metadata`` and ``_runtime_bundle`` before ``extra="forbid"`` runs."""
        return _strip_internal_passthrough_fields(value)

    @model_validator(mode="before")
    @classmethod
    def enforce_max_payload_size(cls, value: object) -> object:
        """Reject payloads whose JSON exceeds ``MAX_GATEWAY_REQUEST_BYTES``.

        Runs before field parsing so oversized bodies never reach Pydantic.
        """
        return _enforce_max_payload_size(value)


class SimulationRequest(GatewayRequestBase):
    """Request model for simulation submission."""

    region: str | None = None

    @model_validator(mode="after")
    def region_xor_region_group(self) -> "SimulationRequest":
        """Reject a request that sets both ``region`` and ``region_group``."""
        if self.region is not None and self.region_group is not None:
            msg = "Provide `region` or `region_group`, not both."
            raise ValueError(msg)
        return self


class PolicyEngineBundle(BaseModel):
    """Resolved runtime provenance returned by the gateway."""

    model_version: str
    policyengine_version: str | None = None
    data_version: str | None = None
    dataset: str | None = None
    dataset_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "SHA-256 the policyengine.py bundle records for the dataset named "
            "in `dataset` (its certified artifact's, or else its dataset "
            "entry's), as registered when this bundle's app was deployed. "
            "Absent for apps deployed before this field existed."
        ),
    )
    spm: SPMCapability | None = None


class SimulationErrorResponse(BaseModel):
    """Existing route errors and public typed SPM input failures."""

    status: Literal["failed"] | None = None
    result: None = None
    error: str | None = None
    errors: list[SPMErrorDetail] | None = None
    detail: str | None = None


class JobSubmitResponse(BaseModel):
    """Response model for job submission."""

    job_id: str
    status: Literal["submitted"]
    poll_url: str
    country: str
    version: str
    resolved_app_name: str
    policyengine_bundle: PolicyEngineBundle


class JobStatusResponse(BaseModel):
    """Response model for job status polling."""

    status: Literal["running", "complete", "failed"]
    result: SingleYearMacroOutput | None = None
    errors: list[SPMErrorDetail] | None = None
    error: str | None = None
    resolved_app_name: str | None = None
    policyengine_bundle: PolicyEngineBundle | None = None
    # Stage reporting. Present only for jobs whose worker reports stages;
    # see policyengine_simulation_contract.job_progress.
    stage: JobStage | None = Field(
        default=None,
        description=(
            "Current stage of a running job, or the stage in which a failed "
            "job stopped. Absent for complete jobs. Later versions may add "
            "stages; clients must accept a value they do not know."
        ),
    )
    progress: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Coarse completion percent at the start of the current stage. It "
            "changes only at stage boundaries, stays below 100 while running, "
            "and is 100 when complete."
        ),
    )
    progress_max: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Highest percent a running job reaches before its next stage "
            "starts. A client can move a time-based estimate between "
            "progress and progress_max."
        ),
    )
    submitted_at: AwareDatetime | None = Field(
        default=None, description="When the gateway accepted the job."
    )
    started_at: AwareDatetime | None = Field(
        default=None, description="When a worker started the job."
    )
    stage_started_at: AwareDatetime | None = Field(
        default=None, description="When the current stage started."
    )
    finished_at: AwareDatetime | None = Field(
        default=None, description="When the worker finished or stopped the job."
    )
    segments: JobSegmentProgress | None = Field(
        default=None,
        description="Region segments of a segmented US national run.",
    )
    stage_durations: dict[JobStage, float] | None = Field(
        default=None,
        description="Seconds spent in each stage that has ended.",
    )


class BudgetWindowBatchRequest(GatewayRequestBase):
    """Request model for budget-window batch submission."""

    MAX_YEARS: ClassVar[int] = 75
    MAX_END_YEAR: ClassVar[int] = 2099
    MAX_PARALLEL: ClassVar[int] = 20

    region: str
    start_year: str
    window_size: int = Field(ge=1, le=MAX_YEARS)
    max_parallel: int = Field(default=MAX_PARALLEL, ge=1, le=MAX_PARALLEL)
    target: Literal["general"] = "general"

    @field_validator("start_year")
    @classmethod
    def validate_start_year(cls, value: str) -> str:
        """Normalise ``start_year`` to a canonical integer string.

        For example ``"02026"`` becomes ``"2026"``; non-integers are rejected.
        """
        try:
            return str(int(value))
        except (TypeError, ValueError) as exc:
            msg = "start_year must be an integer year"
            raise ValueError(msg) from exc

    @model_validator(mode="after")
    def validate_end_year(self) -> "BudgetWindowBatchRequest":
        """Reject windows past ``MAX_END_YEAR``, region groups and cliff requests.

        Budget windows run against a single ``region`` and do not compute cliff
        impacts; those requests belong on the single-year endpoint.
        """
        if self.region_group is not None:
            msg = "Provide `region` or `region_group`, not both."
            raise ValueError(msg)
        end_year = int(self.start_year) + self.window_size - 1
        if end_year > self.MAX_END_YEAR:
            msg = f"budget-window end_year must be {self.MAX_END_YEAR} or earlier"
            raise ValueError(msg)
        if self.include_cliffs is True:
            msg = (
                "budget-window cliff impacts are not supported; use the single-year "
                "simulation endpoint with include_cliffs=true"
            )
            raise ValueError(msg)
        return self


class BudgetWindowAnnualImpact(BaseModel):
    """Annual budget-window impact row."""

    model_config = ConfigDict(extra="forbid")

    spm_provenance: SPMComparisonProvenance | None = None
    year: str
    taxRevenueImpact: float  # noqa: N815  # published budget-window JSON field taxRevenueImpact
    federalTaxRevenueImpact: float  # noqa: N815  # published budget-window JSON field federalTaxRevenueImpact
    stateTaxRevenueImpact: float  # noqa: N815  # published budget-window JSON field stateTaxRevenueImpact
    benefitSpendingImpact: float  # noqa: N815  # published budget-window JSON field benefitSpendingImpact
    budgetaryImpact: float  # noqa: N815  # published budget-window JSON field budgetaryImpact


class BudgetWindowTotals(BaseModel):
    """Aggregate totals for a completed budget-window response."""

    year: Literal["Total"] = "Total"
    taxRevenueImpact: float  # noqa: N815  # published budget-window JSON field taxRevenueImpact
    federalTaxRevenueImpact: float  # noqa: N815  # published budget-window JSON field federalTaxRevenueImpact
    stateTaxRevenueImpact: float  # noqa: N815  # published budget-window JSON field stateTaxRevenueImpact
    benefitSpendingImpact: float  # noqa: N815  # published budget-window JSON field benefitSpendingImpact
    budgetaryImpact: float  # noqa: N815  # published budget-window JSON field budgetaryImpact


class BudgetWindowResult(BaseModel):
    """Completed budget-window output."""

    kind: Literal["budgetWindow"] = "budgetWindow"
    startYear: str  # noqa: N815  # published budget-window JSON field startYear
    endYear: str  # noqa: N815  # published budget-window JSON field endYear
    windowSize: int  # noqa: N815  # published budget-window JSON field windowSize
    annualImpacts: list[BudgetWindowAnnualImpact] = Field(default_factory=list)  # noqa: N815  # published budget-window JSON field annualImpacts
    totals: BudgetWindowTotals


class BatchChildJobStatus(BaseModel):
    """Per-year child simulation job tracking."""

    job_id: str
    status: Literal[
        "pending",
        "queued",
        "running",
        "complete",
        "failed",
        "cancelled",
    ]
    errors: list[SPMErrorDetail] | None = None
    error: str | None = None


class BudgetWindowBatchSubmitResponse(BaseModel):
    """Response model for budget-window batch submission."""

    batch_job_id: str
    status: Literal["submitted"]
    poll_url: str
    country: str
    version: str
    resolved_app_name: str
    policyengine_bundle: PolicyEngineBundle


class BudgetWindowBatchStatusResponse(BaseModel):
    """Response model for budget-window batch polling."""

    status: Literal["submitted", "running", "complete", "failed"]
    progress: int | None = None
    completed_years: list[str] = Field(default_factory=list)
    running_years: list[str] = Field(default_factory=list)
    queued_years: list[str] = Field(default_factory=list)
    failed_years: list[str] = Field(default_factory=list)
    child_jobs: dict[str, BatchChildJobStatus] = Field(default_factory=dict)
    result: BudgetWindowResult | None = None
    errors: list[SPMErrorDetail] | None = None
    error: str | None = None
    resolved_app_name: str | None = None
    policyengine_bundle: PolicyEngineBundle | None = None


class BudgetWindowBatchState(BaseModel):
    """Internal state persisted for a budget-window parent batch job."""

    batch_job_id: str
    status: Literal["submitted", "running", "complete", "failed"]
    country: str
    region: str
    version: str
    target: Literal["general"] = "general"
    resolved_app_name: str
    policyengine_bundle: PolicyEngineBundle
    start_year: str
    window_size: int
    max_parallel: int
    request_payload: JsonObject = Field(default_factory=dict)
    years: list[str] = Field(default_factory=list)
    queued_years: list[str] = Field(default_factory=list)
    running_years: list[str] = Field(default_factory=list)
    completed_years: list[str] = Field(default_factory=list)
    failed_years: list[str] = Field(default_factory=list)
    child_jobs: dict[str, BatchChildJobStatus] = Field(default_factory=dict)
    partial_annual_impacts: dict[str, BudgetWindowAnnualImpact] = Field(
        default_factory=dict
    )
    result: BudgetWindowResult | None = None
    errors: list[SPMErrorDetail] | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    observability_id: str | None = None


class PingRequest(BaseModel):
    """Request model for ping endpoint."""

    value: int


class PingResponse(BaseModel):
    """Response model for ping endpoint."""

    incremented: int


class VersionMap(RootModel[dict[str, str]]):
    """Version alias or exact version mapped to a deployed worker app."""


class VersionsResponse(BaseModel):
    """All supported simulation routing version maps."""

    spm_capabilities: dict[str, SPMCapability] = Field(
        default_factory=dict,
        description=(
            "Canonical SPM capabilities keyed by exact PolicyEngine wrapper version. "
            "Resolve a country-model route to its app in the routing maps, then "
            "find that app's exact version in the policyengine map. App names, "
            "country-model versions and latest aliases are not capability keys."
        ),
    )
    policyengine: VersionMap
    us: VersionMap
    uk: VersionMap
    bundles: dict[str, dict[str, PolicyEngineBundle]] = Field(
        default_factory=dict,
        description=(
            "Deployed bundle identity keyed by exact PolicyEngine wrapper "
            "version, then country: model and data versions, the default "
            "dataset and the SHA-256 the bundle records for it (the certified "
            "artifact's, or the dataset entry's). `policyengine.latest` is a "
            "wrapper version: read its identity at `bundles[latest]`."
        ),
    )


class HealthResponse(BaseModel):
    """Local process health response."""

    status: Literal["healthy"] = "healthy"


class ReadinessResponse(BaseModel):
    """Dependency readiness response."""

    status: Literal["ready", "not_ready"]
