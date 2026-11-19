"""How a simulation's refused policy or dynamic is answered, on every route.

Economy simulations, economy reports and household reports answer a
policy's or dynamic's refusal with one builder, so each says the same thing
and locates it the same way; only what names the values and where they sit
in the request differ.
"""

from __future__ import annotations

import pytest
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionRefusedError,
    EconomySubmissionUnprocessableError,
    simulation_values_refusal,
)
from policyengine_simulation_persistence import (
    MissingPolicyParametersError,
    OverlappingReformError,
    UnknownCatalogVersionError,
    UnknownDynamicError,
    UnknownPolicyError,
)

# The store's own text, which no answer repeats.
STORE_TEXT = "store text that names a row"
ELASTICITY = "gov.simulation.labor_supply_responses.elasticities.income"


def _refusal(error: Exception, *, location: tuple[str, ...] = ("body", "reform")):
    return simulation_values_refusal(
        error,
        country="uk",
        policyengine_version="6.17.1",
        named_by="the request's reform",
        location=location,
    )


@pytest.mark.parametrize(
    ("error", "code", "detail"),
    [
        (
            UnknownPolicyError(STORE_TEXT),
            "unknown_policy",
            "uk has no stored policy the request's reform names.",
        ),
        # A dynamic stored for another country is refused as a missing one.
        (
            UnknownDynamicError(STORE_TEXT),
            "unknown_dynamic",
            "uk has no stored dynamic the request's reform names.",
        ),
    ],
)
def test_a_policy_or_dynamic_the_country_lacks_is_refused_in_the_callers_words(
    error, code, detail
) -> None:
    refusal = _refusal(error)

    assert isinstance(refusal, EconomySubmissionRefusedError)
    assert (refusal.code, refusal.detail) == (code, detail)
    assert STORE_TEXT not in refusal.detail


@pytest.mark.parametrize(
    ("error", "code", "field_name", "reason_type"),
    [
        (
            MissingPolicyParametersError([ELASTICITY], source="policy"),
            "missing_parameters",
            "policy_id",
            "missing_parameter",
        ),
        (
            MissingPolicyParametersError([ELASTICITY], source="dynamic"),
            "missing_parameters",
            "dynamic_id",
            "missing_parameter",
        ),
        (
            OverlappingReformError([ELASTICITY]),
            "overlapping_parameters",
            "dynamic_id",
            "overlapping_parameter",
        ),
    ],
    ids=["policy-lacks", "dynamic-lacks", "overlapping"],
)
@pytest.mark.parametrize(
    "location", [("body",), ("report", "baseline")], ids=["own", "report-side"]
)
def test_values_that_cannot_be_calculated_are_located_at_the_field_that_names_them(
    error, code, field_name, reason_type, location
) -> None:
    refusal = _refusal(error, location=location)

    assert isinstance(refusal, EconomySubmissionUnprocessableError)
    assert refusal.code == code
    ((reason),) = refusal.reasons
    assert (reason.loc, reason.type) == (
        (*location, field_name, ELASTICITY),
        reason_type,
    )


def test_an_error_of_another_kind_is_left_to_its_own_refusal() -> None:
    assert _refusal(UnknownCatalogVersionError(STORE_TEXT)) is None
