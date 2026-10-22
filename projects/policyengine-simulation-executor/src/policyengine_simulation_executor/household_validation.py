"""The household that checks a v2 household function calculates.

Release validation calls each deployed household function with this
household, and the pre-merge image check calculates it inside the household
image. Both use ``check_household_validation`` to decide whether it worked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionRequest,
    HouseholdExecutionResult,
    parse_household_execution_outcome,
)

from policyengine_simulation_executor.household_calculation import (
    installed_household_bundle,
)

if TYPE_CHECKING:
    from policyengine_simulation_contract.household_execution import (
        HouseholdWorkerIdentity,
    )
    from policyengine_simulation_contract.stage12_bundle import CountryId

# The group entity, besides the household, that holds the adult in each
# country's validation household.
_ADULT_GROUP_PLURALS: dict[str, str] = {"us": "spm_units", "uk": "benunits"}


def household_validation_request(
    country: CountryId, *, year: int
) -> HouseholdExecutionRequest:
    """Return a one-adult, current-law household request for ``country``.

    The request names the installed bundle and asks for the household's net
    income in ``year``.
    """
    period = str(year)
    household = {
        "people": {"you": {"age": {period: 40}, "employment_income": {period: 30_000}}},
        _ADULT_GROUP_PLURALS[country]: {"your unit": {"members": ["you"]}},
        "households": {
            "your household": {
                "members": ["you"],
                "household_net_income": {period: None},
            }
        },
    }
    return HouseholdExecutionRequest.model_validate(
        {
            "calculation_id": uuid4(),
            "country": country,
            "household": household,
            "policies": [{"policy_key": "baseline", "parameters": {}}],
            "bundle": installed_household_bundle(country),
        }
    )


def check_household_validation(
    outcome: object,
    *,
    request: HouseholdExecutionRequest,
    worker: HouseholdWorkerIdentity,
) -> float:
    """Return the household net income ``outcome`` calculated for ``request``.

    Raises RuntimeError unless the outcome is a success with the request's
    bundle, reported by ``worker``, with a number for the household's net
    income.
    """
    name = worker.function_name
    parsed = parse_household_execution_outcome(outcome)
    if not isinstance(parsed, HouseholdExecutionResult):
        msg = f"{name} did not calculate the validation household"
        raise RuntimeError(msg)  # noqa: TRY004  # a failed calculation fails the check; it is not a type error
    if parsed.output.bundle != request.bundle:
        msg = f"{name} calculated with another bundle"
        raise RuntimeError(msg)
    if parsed.worker != worker:
        msg = f"{name} reported another function"
        raise RuntimeError(msg)
    situation = parsed.output.results[0].household.to_situation()
    net_income = situation["households"]["your household"]["household_net_income"]
    values = list(net_income.values()) if isinstance(net_income, dict) else []
    if len(values) != 1 or not isinstance(values[0], float):
        msg = f"{name} returned no household net income"
        raise RuntimeError(msg)
    return values[0]
