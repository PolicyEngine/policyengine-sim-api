"""
Pydantic models for the Gateway API.
"""

import json
from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from policyengine_simulation_executor.telemetry import TelemetryEnvelope


# Hard cap on request body size (bytes). SimulationOptions + telemetry + any
# reform/baseline parameter tree should fit comfortably in ~256 KB. A hostile
# client that tries to stream a multi-MB reform dict is rejected with 422
# before Pydantic allocates proxy objects, which prevents memory-based DoS
# against the gateway ASGI worker. If we discover a legitimate use case that
# exceeds this we should add an explicit endpoint for bulk parameter upload
# rather than relaxing the cap here.
MAX_GATEWAY_REQUEST_BYTES = 262_144


INTERNAL_PASSTHROUGH_FIELDS = frozenset({"_metadata", "_runtime_bundle"})


def _move_internal_telemetry_alias(value):
    if not isinstance(value, dict):
        return value
    if "_telemetry" in value and "telemetry" not in value:
        value = dict(value)
        value["telemetry"] = value.pop("_telemetry")
    return value


def _strip_internal_passthrough_fields(value):
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


def _enforce_max_payload_size(value):
    if not isinstance(value, dict):
        return value
    try:
        encoded_length = len(json.dumps(value, default=str))
    except TypeError:
        # Non-JSON-serialisable values will fail later in Pydantic; size cap
        # only guards against well-formed hostile payloads.
        return value
    if encoded_length > MAX_GATEWAY_REQUEST_BYTES:
        raise ValueError(
            f"Request body is too large ({encoded_length} bytes); the gateway "
            f"accepts at most {MAX_GATEWAY_REQUEST_BYTES} bytes."
        )
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
    version: Optional[str] = None
    policyengine_version: Optional[str] = None
    telemetry: TelemetryEnvelope | None = None

    # Fields forwarded to SimulationOptions on the worker side.
    scope: Optional[str] = None
    data: Optional[str] = None
    time_period: Optional[str] = None
    reform: Optional[dict[str, Any]] = None
    baseline: Optional[dict[str, Any]] = None
    region: Optional[str] = None
    title: Optional[str] = None
    include_cliffs: Optional[bool] = None
    model_version: Optional[str] = None
    data_version: Optional[str] = None

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    @model_validator(mode="before")
    @classmethod
    def move_internal_telemetry_alias(cls, value):
        return _move_internal_telemetry_alias(value)

    @model_validator(mode="before")
    @classmethod
    def strip_internal_passthrough_fields(cls, value):
        return _strip_internal_passthrough_fields(value)

    @model_validator(mode="before")
    @classmethod
    def enforce_max_payload_size(cls, value):
        return _enforce_max_payload_size(value)


class SimulationRequest(GatewayRequestBase):
    """Request model for simulation submission."""


class PolicyEngineBundle(BaseModel):
    """Resolved runtime provenance returned by the gateway."""

    model_version: str
    policyengine_version: Optional[str] = None
    data_version: Optional[str] = None
    dataset: Optional[str] = None


class JobSubmitResponse(BaseModel):
    """Response model for job submission."""

    job_id: str
    status: str
    poll_url: str
    country: str
    version: str
    resolved_app_name: str
    policyengine_bundle: PolicyEngineBundle
    run_id: Optional[str] = None


class JobStatusResponse(BaseModel):
    """Response model for job status polling."""

    status: str
    result: Optional[dict] = None
    error: Optional[str] = None
    resolved_app_name: Optional[str] = None
    policyengine_bundle: Optional[PolicyEngineBundle] = None
    run_id: Optional[str] = None


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
        try:
            return str(int(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("start_year must be an integer year") from exc

    @model_validator(mode="after")
    def validate_end_year(self) -> "BudgetWindowBatchRequest":
        end_year = int(self.start_year) + self.window_size - 1
        if end_year > self.MAX_END_YEAR:
            raise ValueError(
                f"budget-window end_year must be {self.MAX_END_YEAR} or earlier"
            )
        if self.include_cliffs is True:
            raise ValueError(
                "budget-window cliff impacts are not supported; use the single-year "
                "simulation endpoint with include_cliffs=true"
            )
        return self


class BudgetWindowAnnualImpact(BaseModel):
    """Annual budget-window impact row."""

    year: str
    taxRevenueImpact: float
    federalTaxRevenueImpact: float
    stateTaxRevenueImpact: float
    benefitSpendingImpact: float
    budgetaryImpact: float


class BudgetWindowTotals(BaseModel):
    """Aggregate totals for a completed budget-window response."""

    year: Literal["Total"] = "Total"
    taxRevenueImpact: float
    federalTaxRevenueImpact: float
    stateTaxRevenueImpact: float
    benefitSpendingImpact: float
    budgetaryImpact: float


class BudgetWindowResult(BaseModel):
    """Completed budget-window output."""

    kind: Literal["budgetWindow"] = "budgetWindow"
    startYear: str
    endYear: str
    windowSize: int
    annualImpacts: list[BudgetWindowAnnualImpact] = Field(default_factory=list)
    totals: BudgetWindowTotals


class BatchChildJobStatus(BaseModel):
    """Per-year child simulation job tracking."""

    job_id: str
    status: str
    error: Optional[str] = None


class BudgetWindowBatchSubmitResponse(BaseModel):
    """Response model for budget-window batch submission."""

    batch_job_id: str
    status: str
    poll_url: str
    country: str
    version: str
    resolved_app_name: str
    policyengine_bundle: PolicyEngineBundle
    run_id: Optional[str] = None


class BudgetWindowBatchStatusResponse(BaseModel):
    """Response model for budget-window batch polling."""

    status: str
    progress: Optional[int] = None
    completed_years: list[str] = Field(default_factory=list)
    running_years: list[str] = Field(default_factory=list)
    queued_years: list[str] = Field(default_factory=list)
    failed_years: list[str] = Field(default_factory=list)
    child_jobs: dict[str, BatchChildJobStatus] = Field(default_factory=dict)
    result: Optional[BudgetWindowResult] = None
    error: Optional[str] = None
    resolved_app_name: Optional[str] = None
    policyengine_bundle: Optional[PolicyEngineBundle] = None
    run_id: Optional[str] = None


class BudgetWindowBatchState(BaseModel):
    """Internal state persisted for a budget-window parent batch job."""

    batch_job_id: str
    status: str
    country: str
    region: str
    version: str
    target: Literal["general"] = "general"
    resolved_app_name: str
    policyengine_bundle: PolicyEngineBundle
    start_year: str
    window_size: int
    max_parallel: int
    request_payload: dict[str, Any] = Field(default_factory=dict)
    years: list[str] = Field(default_factory=list)
    queued_years: list[str] = Field(default_factory=list)
    running_years: list[str] = Field(default_factory=list)
    completed_years: list[str] = Field(default_factory=list)
    failed_years: list[str] = Field(default_factory=list)
    child_jobs: dict[str, BatchChildJobStatus] = Field(default_factory=dict)
    partial_annual_impacts: dict[str, BudgetWindowAnnualImpact] = Field(
        default_factory=dict
    )
    result: Optional[BudgetWindowResult] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str
    run_id: Optional[str] = None


class PingRequest(BaseModel):
    """Request model for ping endpoint."""

    value: int


class PingResponse(BaseModel):
    """Response model for ping endpoint."""

    incremented: int
