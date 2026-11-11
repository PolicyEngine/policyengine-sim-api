"""Which request field a refused field of the household contract's request traces to."""

from __future__ import annotations

import pytest
from policyengine_simulation_entry.household_contract_fields import request_field_of


@pytest.mark.parametrize(
    ("location", "spm_from_request", "request_field"),
    [
        # The stored household, and its default year, come from household_id.
        (("household", "people", "you", "age"), False, "household_id"),
        (("household", "families", "a family", "members"), False, "household_id"),
        (("year",), False, "household_id"),
        # The request's axes are put into the household.
        (("household", "axes", 0, 0, "count"), False, "axes"),
        (
            ("policy", "parameters", "gov.x", "2026-01-01.2200-12-31"),
            False,
            "policy_id",
        ),
        # SPM settings are the request's, or else the stored household's.
        (("spm",), True, "spm"),
        (("spm",), False, "household_id"),
        (("country",), False, "country"),
        (("add_missing_variables",), False, "add_missing_variables"),
        # The service builds these itself; no request field is to blame.
        (("bundle", "core_package_version"), False, None),
        (("calculation_id",), False, None),
        (("spm_selection_source",), True, None),
        (("schema_version",), False, None),
        ((), False, None),
    ],
)
def test_each_refused_contract_field_traces_to_the_request_field_that_set_it(
    location, spm_from_request, request_field
):
    assert request_field_of(location, spm_from_request=spm_from_request) == (
        request_field
    )
