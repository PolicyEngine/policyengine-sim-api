"""Build a household report run's household function payload from its inputs.

Each side's request calculates the stored household, in its country, for
the report's year with the household's saved SPM selection, on the default
worker's bundle; its calculation ID is the side's simulation run. The
household report services (``household_report_submissions``,
``household_report_reruns``) build it before writing anything, on the run
IDs a request proposes, and again for a run they created, on the runs it
names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionRequest,
)
from policyengine_simulation_contract.household_report_execution import (
    HouseholdReportRunCalculation,
)
from pydantic import ValidationError

from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionUnprocessableError,
    UnprocessableReason,
)
from policyengine_simulation_entry.household_contract_fields import (
    job_policy_fields,
    request_field_of,
)
from policyengine_simulation_entry.household_conversion import (
    UnreadableHouseholdError,
    household_situation,
    saved_spm_selection,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from policyengine_simulation_persistence import (
        HouseholdReportInputs,
        PolicyReform,
        ReportSide,
        StoredHousehold,
    )

    from policyengine_simulation_entry.household_workers import HouseholdWorker


class HouseholdReportBuildError(Exception):
    """The household function's contract refused a field the service built.

    The message names each refused field, never a value; the route answers
    it as an internal error.
    """


def household_not_calculable(
    *reasons: UnprocessableReason,
) -> EconomySubmissionUnprocessableError:
    """Return the 422 refusing a household report that cannot be calculated."""
    return EconomySubmissionUnprocessableError(
        code="household_not_calculable", reasons=reasons
    )


def _spm_fields(document: Mapping[str, Any]) -> dict[str, object]:
    """Return the contract's SPM fields: the household's saved selection, if any."""
    saved = saved_spm_selection(document)
    if saved is None:
        return {}
    return {"spm": saved, "spm_selection_source": "saved_household"}


def _contract_reasons(
    error: ValidationError, side: ReportSide
) -> tuple[UnprocessableReason, ...]:
    """Name each field the contract refuses by the request field it traces to.

    Raise HouseholdReportBuildError when the contract refuses a field the
    service builds itself, which is no fault of the caller's.
    """
    reasons = []
    built_wrong = []
    for item in error.errors():
        location = tuple(item["loc"])
        request_field = request_field_of(location, spm_from_request=False)
        # A household report sets add_missing_variables itself.
        if request_field is None or request_field == "add_missing_variables":
            built_wrong.append(".".join(str(part) for part in location) or "request")
            continue
        field_location = (
            (side, request_field) if request_field == "policy_id" else (request_field,)
        )
        reasons.append(
            UnprocessableReason(
                loc=("body", *field_location, *location),
                msg=item["msg"],
                type=item["type"],
            )
        )
    if built_wrong:
        msg = (
            "the household function's request has fields the service built "
            f"wrong: {', '.join(built_wrong)}"
        )
        raise HouseholdReportBuildError(msg)
    return tuple(reasons)


@dataclass(frozen=True)
class HouseholdReportSideInputs:
    """One side of a household report run: the simulation run it records.

    ``policy_values`` holds the side's policy values by parameter name and
    period, empty for current law, as ``HouseholdReportStore.read_inputs``
    reads them into ``reforms``.
    """

    simulation_run_id: UUID
    policy_values: PolicyReform


def side_runs_calculation(
    inputs: HouseholdReportInputs,
    *,
    year: int,
    run_ids: Mapping[ReportSide, UUID],
    worker: HouseholdWorker,
) -> HouseholdReportRunCalculation:
    """Build the household function payload for these side runs of ``inputs``.

    ``run_ids`` names each side's simulation run, the baseline's and any
    reform's; raise as ``household_report_calculation`` does.
    """
    sides = {
        side: HouseholdReportSideInputs(
            simulation_run_id=run_id, policy_values=inputs.reforms[side]
        )
        for side, run_id in run_ids.items()
    }
    return household_report_calculation(
        household=inputs.household,
        year=year,
        baseline=sides["baseline"],
        reform=sides.get("reform"),
        worker=worker,
    )


def household_report_calculation(
    *,
    household: StoredHousehold,
    year: int,
    baseline: HouseholdReportSideInputs,
    reform: HouseholdReportSideInputs | None = None,
    worker: HouseholdWorker,
) -> HouseholdReportRunCalculation:
    """Build a household report run's household function payload from its inputs.

    Each side's request calculates the stored ``household``, in its
    country, for ``year`` with the household's saved SPM selection, on
    ``worker``'s bundle, and its calculation ID is the side's simulation
    run; ``reform`` is None for a report of the baseline alone. Every side
    adds the household's missing variables, so a household report holds
    every value. Creating a report passes the household's default year. A
    restart or re-run passes the stored report's year, and the household
    and policy values as they are now read.

    Raise EconomySubmissionUnprocessableError (``household_not_calculable``)
    when the stored household cannot be read, or the household contract
    refuses what would be calculated, the baseline's checked first. Its
    reasons are located at a report request's fields (``household_id``, a
    side's ``policy_id``); a caller rebuilding a stored run reads them as
    its own. Raise HouseholdReportBuildError when the contract refuses a
    field the service builds, two sides recording one simulation run
    included.
    """
    country = household.country
    try:
        situation = household_situation(country, household.document)
    except UnreadableHouseholdError:
        raise household_not_calculable(
            UnprocessableReason(
                loc=("body", "household_id"),
                msg="the stored household's people or groups cannot be read",
                type="household_unreadable",
            )
        ) from None
    sides: dict[ReportSide, HouseholdReportSideInputs] = {"baseline": baseline}
    if reform is not None:
        sides["reform"] = reform
    requests = {
        side: _side_request(
            side,
            side_inputs,
            year=year,
            situation=situation,
            household=household,
            worker=worker,
        )
        for side, side_inputs in sides.items()
    }
    try:
        return HouseholdReportRunCalculation(
            baseline=requests["baseline"], reform=requests.get("reform")
        )
    except ValidationError as error:
        msg = "the household report run's calculation was built wrong: " + "; ".join(
            str(item["msg"]) for item in error.errors()
        )
        raise HouseholdReportBuildError(msg) from None


def _side_request(  # noqa: PLR0913  # the side and what it calculates
    side: ReportSide,
    side_inputs: HouseholdReportSideInputs,
    *,
    year: int,
    situation: dict[str, Any],
    household: StoredHousehold,
    worker: HouseholdWorker,
) -> HouseholdExecutionRequest:
    """Build one side's household contract request; raise its refusal."""
    try:
        return HouseholdExecutionRequest.model_validate(
            {
                "calculation_id": side_inputs.simulation_run_id,
                "country": household.country,
                # The report's year, as a situation's period key.
                "year": str(year),
                "household": situation,
                **job_policy_fields(reform=side_inputs.policy_values),
                # A household report holds every value, as the Public API's
                # stored-household route gives it.
                "add_missing_variables": True,
                "bundle": worker.bundle,
                **_spm_fields(household.document),
            }
        )
    except ValidationError as error:
        raise household_not_calculable(*_contract_reasons(error, side)) from None
