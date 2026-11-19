"""How the household path's request maps onto the household contract's request.

``policyengine_simulation_contract.household_execution`` defines the request
a household function takes. This module holds what the service depends on
about its fields: how the request names the job's one policy
(``job_policy_fields``), and which field of the service's own request each
of its fields comes from (``request_field_of``), so that a refusal names the
caller's field or, for a field the service builds itself, is an internal
error. The contract's one policy holds the stored policy's values and the
stored dynamic's together, so a refused value traces to whichever of the two
sets its parameter. A change to the contract's fields changes this module
alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from policyengine_simulation_persistence import PolicyReform

# The request's top-level fields that hold the policy.
POLICY_FIELDS: Final = frozenset({"policy"})
# The request field each of the contract request's top-level fields is built
# from, apart from the axes and SPM settings; the service builds the rest.
_REQUEST_FIELDS: Final = {
    "household": "household_id",
    "year": "household_id",
    "country": "country",
    "add_missing_variables": "add_missing_variables",
    **dict.fromkeys(POLICY_FIELDS, "policy_id"),
}


def job_policy_fields(*, reform: PolicyReform) -> dict[str, object]:
    """Return the request fields naming the job's one policy and its values.

    Current law changes no parameter, so its reform is empty.
    """
    return {"policy": {"parameters": reform}}


def request_field_of(
    location: tuple[str | int, ...],
    *,
    spm_from_request: bool,
    dynamic_parameters: frozenset[str],
) -> str | None:
    """Return the request field a refused field of the contract's request came from.

    The stored household, and its default year, come from ``household_id``,
    except the request's own axes; the policy from ``policy_id``, except a
    value of a parameter in ``dynamic_parameters``, which the stored dynamic
    sets, from ``dynamic_id``; SPM settings from ``spm`` when the request
    sent them, else from the stored household. Return None for a field the
    service builds itself, such as the bundle or the calculation ID: its
    refusal is the service's fault.
    """
    top_field = location[0] if location else None
    if top_field == "household" and location[1:2] == ("axes",):
        return "axes"
    if top_field == "spm":
        return "spm" if spm_from_request else "household_id"
    if (
        top_field in POLICY_FIELDS
        and location[1:2] == ("parameters",)
        and location[2:3]
        and location[2] in dynamic_parameters
    ):
        return "dynamic_id"
    return _REQUEST_FIELDS.get(str(top_field))
